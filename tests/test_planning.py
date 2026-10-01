"""P1 收尾（周期记账 / 预算 / 债务）与 P3（K 线与指标）用例。

重点全部落在**会算错且不报错**的地方：
月末与闰年的夹取、预算的结转方向、本金与利息的分账、
K 线的 open 取哪一天、以及"指标没有预热就是错的"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from accountbook.core.domain import DebtKind, TransactionType
from accountbook.core.errors import ConflictError, NotFoundError, ValidationError
from accountbook.core.periods import add_months, clamp_day, period_bounds, shift_period, week_start
from accountbook.db.migrations import run_migrations
from accountbook.db.models import AssetOhlc
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import budgets as budgets_service
from accountbook.services import categories as categories_service
from accountbook.services import debts as debts_service
from accountbook.services import kline as kline_service
from accountbook.services import recurring as recurring_service
from accountbook.services import stats as stats_service
from accountbook.services import transactions as transactions_service
from accountbook.services.transactions import TransactionQuery

TODAY = date.today()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ledger.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database):
    with db.session() as active:
        yield active


def _account(session: Session, index: int = 0):
    return accounts_service.list_accounts(session)[index]


def _category(session: Session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    raise AssertionError(f"未找到分类 {name}")


def _spend(session: Session, *, day: date, amount: int, category: str = "午餐", account_index: int = 0):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_account(session, account_index).id,
        category_id=_category(session, category),
        amount_minor=amount,
        occurred_at=datetime.combine(day, datetime.min.time()).replace(hour=12),
    )


# =============================================================================
# 期间推算
# =============================================================================
class TestPeriods:
    def test_month_end_clamping(self) -> None:
        """1 月 31 日 +1 月 必须落在 2 月最后一天，而不是抛错或跳到 3 月。"""
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29), "闰年要落在 29 日"

    def test_month_end_marker(self) -> None:
        """``-1`` 表示月末。写成 31 会让 2 月、4 月、6 月永远不触发。"""
        for month in (2, 4, 6, 9, 11):
            result = clamp_day(2026, month, -1)
            assert result.month == month, f"{month} 月不能溢出到下个月"
            assert result.day >= 28

    def test_year_shift_on_leap_day(self) -> None:
        assert shift_period("yearly", date(2024, 2, 29), 1) == date(2025, 2, 28)

    def test_week_starts_monday(self) -> None:
        """以周一为一周之首：周日开始会把"周末消费"劈成两半。"""
        assert week_start(date(2026, 10, 1)).weekday() == 0
        assert period_bounds("weekly", date(2026, 10, 1)) == (date(2026, 9, 28), date(2026, 10, 4))

    def test_quarter_bounds(self) -> None:
        assert period_bounds("quarterly", date(2026, 8, 15)) == (date(2026, 7, 1), date(2026, 9, 30))


# =============================================================================
# 周期记账
# =============================================================================
class TestRecurring:
    def _rule(self, session: Session, **overrides):
        payload = {
            "name": "房租",
            "type": TransactionType.EXPENSE.value,
            "account_id": _account(session).id,
            "category_id": _category(session, "房租"),
            "amount_minor": 450_000,
            "frequency": "monthly",
            "start_date": date(2026, 1, 31),
            "auto_post": True,
        }
        payload.update(overrides)
        return recurring_service.create_rule(session, **payload)

    def test_monthly_31st_does_not_drift(self, session: Session) -> None:
        """每次从起点重算，而不是"上次加一个月" —— 后者会让 31 日永久变成 28 日。"""
        rule = self._rule(session)
        first = date(2026, 1, 31)
        second = recurring_service.next_occurrence(rule, first)
        third = recurring_service.next_occurrence(rule, second)
        fourth = recurring_service.next_occurrence(rule, third)
        assert second == date(2026, 2, 28)
        assert third == date(2026, 3, 31), "2 月被夹到 28 后，3 月必须回到 31 日"
        assert fourth == date(2026, 4, 30)

    def test_month_end_marker_stays_at_month_end(self, session: Session) -> None:
        rule = self._rule(session, by_month_day=-1)
        assert recurring_service.next_occurrence(rule, date(2026, 1, 31)) == date(2026, 2, 28)
        assert recurring_service.next_occurrence(rule, date(2026, 2, 28)) == date(2026, 3, 31)

    def test_start_date_in_the_middle_of_month(self, session: Session) -> None:
        """ "每月 5 日"、起始日 1 月 15 日：第一次必须是 2 月 5 日，不能是已过去的 1 月 5 日。"""
        rule = self._rule(session, start_date=date(2026, 1, 15), by_month_day=5)
        assert rule.next_due_date == date(2026, 2, 5)

    def test_weekly_aligns_to_weekday(self, session: Session) -> None:
        rule = self._rule(
            session,
            frequency="weekly",
            start_date=date(2026, 1, 7),  # 周三
            by_month_day=None,
            by_weekday=4,  # 周五
        )
        assert rule.next_due_date == date(2026, 1, 9)

    def test_end_date_stops_the_series(self, session: Session) -> None:
        rule = self._rule(session, end_date=date(2026, 2, 28))
        assert recurring_service.next_occurrence(rule, date(2026, 2, 28)) is None

    def test_post_due_creates_transactions(self, session: Session) -> None:
        self._rule(session)
        # 3 月 31 日还没到，因此只应生成 1/31 与 2/28 两期 ——
        # "到达日期才生成"正是周期记账该有的行为
        report = recurring_service.post_due(session, on=date(2026, 3, 15))
        assert len(report["created"]) == 2, "3/15 时 3/31 还没到，不该提前生成"

        report = recurring_service.post_due(session, on=date(2026, 3, 31))
        assert len(report["created"]) == 1, "到 3/31 再补上第三期"
        created, total = transactions_service.list_transactions(session, TransactionQuery(limit=100))
        assert total == 3
        assert all(item.source == "recurring" for item in created)

    def test_manual_confirm_rules_are_not_posted(self, session: Session) -> None:
        """只提醒不记账的规则不能被自动生成 —— 信用卡还款金额每月不同，替你记会记错。"""
        self._rule(session, auto_post=False)
        report = recurring_service.post_due(session, on=date(2026, 3, 15))
        assert report["created"] == []
        assert any(item["reason"] == "manual_confirm" for item in report["skipped"])
        assert transactions_service.list_transactions(session, TransactionQuery(limit=10))[1] == 0

    def test_dry_run_writes_nothing(self, session: Session) -> None:
        self._rule(session)
        report = recurring_service.post_due(session, on=date(2026, 3, 15), dry_run=True)
        assert len(report["created"]) == 2, "3/15 时只有 1/31 与 2/28 到期"
        assert transactions_service.list_transactions(session, TransactionQuery(limit=10))[1] == 0

    def test_catch_up_is_capped(self, session: Session) -> None:
        """停了很久的每日规则不能凭空生成几百笔 —— 那比不生成更难收拾。"""
        self._rule(
            session,
            frequency="daily",
            start_date=date(2026, 1, 1),
            by_month_day=None,
            amount_minor=100,
        )
        report = recurring_service.post_due(session, on=date(2026, 12, 31))
        assert len(report["created"]) == recurring_service.MAX_CATCH_UP
        assert any(item["reason"] == "too_many_pending" for item in report["skipped"])

    def test_upcoming_includes_overdue(self, session: Session) -> None:
        self._rule(session, auto_post=False)
        items = recurring_service.upcoming(session, within_days=7, on=date(2026, 6, 1))
        assert items and all(item["overdue"] for item in items), "逾期项必须留在视野里"

    def test_invalid_reference_rejected(self, session: Session) -> None:
        """账户/分类要在**保存时**校验：让它到生成那天才失败，用户会白等一个月。"""
        with pytest.raises(NotFoundError):
            self._rule(session, account_id=999_999)
        with pytest.raises(NotFoundError):
            self._rule(session, category_id=999_999)

    def test_transfer_requires_target(self, session: Session) -> None:
        with pytest.raises(ValidationError, match="目标账户"):
            self._rule(session, type=TransactionType.TRANSFER.value, to_account_id=None)


# =============================================================================
# 预算
# =============================================================================
class TestBudgets:
    def _budget(self, session: Session, **overrides):
        payload = {
            "name": "本月总额",
            "scope": "total",
            "period": "monthly",
            "amount_minor": 100_000,
            "start_date": TODAY.replace(day=1),
            "end_date": TODAY,
        }
        payload.update(overrides)
        return budgets_service.create_budget(session, **payload)

    def test_zero_data_state(self, session: Session) -> None:
        """没有支出时必须显示"还剩全部额度"，而不是"已用完"。"""
        budget = self._budget(session)
        status = budgets_service.budget_status(session, budget, on=TODAY)
        assert status["spent_minor"] == 0
        assert status["remaining_minor"] == 100_000
        assert status["ratio"] == 0.0
        assert status["over"] is False

    def test_spending_counts_splits(self, session: Session) -> None:
        """分账优先：一笔拆到"午餐 70 + 打车 30"的流水，两类各计各的。"""
        lunch = _category(session, "午餐")
        taxi = _category(session, "打车")
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=_account(session).id,
            amount_minor=10_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=12),
            splits=[
                {"category_id": lunch, "amount_minor": 7_000},
                {"category_id": taxi, "amount_minor": 3_000},
            ],
        )
        lunch_budget = self._budget(
            session, name="午餐", scope="category", category_id=lunch, amount_minor=10_000
        )
        status = budgets_service.budget_status(session, lunch_budget, on=TODAY)
        assert status["spent_minor"] == 7_000, "只应计入属于该分类的那一部分"
        assert status["remaining_minor"] == 3_000

    def test_transfers_are_excluded(self, session: Session) -> None:
        budget = self._budget(session)
        accounts = accounts_service.list_accounts(session)
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=accounts[0].id,
            to_account_id=accounts[1].id,
            amount_minor=50_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=12),
        )
        status = budgets_service.budget_status(session, budget, on=TODAY)
        assert status["spent_minor"] == 0, "转账不是支出"

    def test_ratio_is_not_clamped(self, session: Session) -> None:
        """超支时 ratio 要能大于 1 —— "用掉 130%" 与 "用掉 100%" 是不同处境。"""
        budget = self._budget(session, amount_minor=10_000)
        _spend(session, day=TODAY, amount=13_000)
        status = budgets_service.budget_status(session, budget, on=TODAY)
        assert status["ratio"] == pytest.approx(1.3)
        assert status["remaining_minor"] == -3_000
        assert status["over"] is True

    def test_rollover_only_carries_positive(self, session: Session) -> None:
        """上期超支不该吃掉本期额度，否则一次意外支出会连续惩罚两个期间。"""
        budget = self._budget(session, rollover=True, amount_minor=100_000)
        previous_month = shift_period("monthly", TODAY, -1)
        _spend(session, day=previous_month, amount=130_000)  # 上期超支 3 万
        status = budgets_service.budget_status(session, budget, on=TODAY)
        assert status["carryover_minor"] == 0

    def test_rollover_carries_surplus(self, session: Session) -> None:
        budget = self._budget(session, rollover=True, amount_minor=100_000)
        previous_month = shift_period("monthly", TODAY, -1)
        _spend(session, day=previous_month, amount=60_000)
        status = budgets_service.budget_status(session, budget, on=TODAY)
        assert status["carryover_minor"] == 40_000
        assert status["available_minor"] == 140_000

    def test_custom_period_requires_dates(self, session: Session) -> None:
        with pytest.raises(ValidationError, match="起止日期"):
            self._budget(session, period="custom", start_date=None, end_date=None)

    def test_category_budget_requires_category(self, session: Session) -> None:
        with pytest.raises(ValidationError, match="指定分类"):
            self._budget(session, scope="category", category_id=None)

    def test_daily_allowance_uses_remaining_days(self, session: Session) -> None:
        budget = self._budget(session, amount_minor=300_000)
        status = budgets_service.budget_status(session, budget, on=TODAY.replace(day=1))
        month_end = period_bounds("monthly", TODAY)[1]
        assert status["days_left"] == (month_end - TODAY.replace(day=1)).days + 1
        assert status["daily_allowance_minor"] > 0

    def test_overview_separates_total_from_category(self, session: Session) -> None:
        """总预算只取 scope=total 的那条：把分类预算相加会得出用户没设过的数。"""
        self._budget(session)
        self._budget(session, name="午餐", scope="category", category_id=_category(session, "午餐"))
        payload = budgets_service.overview(session, on=TODAY)
        assert payload["total"]["name"] == "本月总额"
        assert len(payload["category_budgets"]) == 1
        assert payload["has_budget"] is True


# =============================================================================
# 债务
# =============================================================================
class TestDebts:
    def _debt(self, session: Session, **overrides):
        payload = {
            "name": "借给小王",
            "kind": DebtKind.LEND.value,
            "counterparty": "小王",
            "principal_minor": 500_000,
            "start_date": TODAY - timedelta(days=30),
            "due_date": TODAY + timedelta(days=30),
        }
        payload.update(overrides)
        return debts_service.create_debt(session, **payload)

    def test_initial_state(self, session: Session) -> None:
        debt = self._debt(session)
        status = debts_service.debt_status(session, debt, on=TODAY)
        assert status["remaining_minor"] == 500_000
        assert status["progress"] == 0.0
        assert status["overdue"] is False

    def test_principal_and_interest_are_separate(self, session: Session) -> None:
        """混成一个数字后就再也算不出"还剩多少本金"—— 而那正是最核心的一个数。"""
        debt = self._debt(session)
        debts_service.add_payment(
            session,
            debt.id,
            amount_minor=100_000,
            principal_minor=95_000,
            interest_minor=5_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=10),
        )
        status = debts_service.debt_status(session, debt, on=TODAY)
        assert status["paid_principal_minor"] == 95_000
        assert status["paid_interest_minor"] == 5_000
        assert status["remaining_minor"] == 405_000

    def test_payment_defaults_to_principal(self, session: Session) -> None:
        """未指明时整笔视为本金：记成利息会凭空产生支出，记成本金只是保守。"""
        debt = self._debt(session)
        payment = debts_service.add_payment(
            session,
            debt.id,
            amount_minor=50_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()),
        )
        assert payment.principal_minor == 50_000
        assert payment.interest_minor == 0

    def test_mismatched_split_rejected(self, session: Session) -> None:
        debt = self._debt(session)
        with pytest.raises(ValidationError, match="之和"):
            debts_service.add_payment(
                session,
                debt.id,
                amount_minor=10_000,
                principal_minor=6_000,
                interest_minor=5_000,
                occurred_at=datetime.combine(TODAY, datetime.min.time()),
            )

    def test_auto_settle_and_revert(self, session: Session) -> None:
        debt = self._debt(session, principal_minor=100_000)
        payment = debts_service.add_payment(
            session,
            debt.id,
            amount_minor=100_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()),
        )
        assert debt.status == "settled"
        assert debt.settled_at == TODAY

        debts_service.delete_payment(session, payment.id)
        session.refresh(debt)
        assert debt.status == "active", "删掉还款后状态必须回退，否则钱还不回去了"
        assert debt.settled_at is None

    def test_settled_debt_rejects_new_payment(self, session: Session) -> None:
        debt = self._debt(session, principal_minor=10_000)
        debts_service.add_payment(
            session,
            debt.id,
            amount_minor=10_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()),
        )
        with pytest.raises(ConflictError):
            debts_service.add_payment(
                session,
                debt.id,
                amount_minor=1_000,
                occurred_at=datetime.combine(TODAY, datetime.min.time()),
            )

    def test_overpay_is_visible(self, session: Session) -> None:
        """多还通常是记错了或对方退了钱，把它夹成 0 会让人对不上账。"""
        debt = self._debt(session, principal_minor=10_000)
        debts_service.add_payment(
            session,
            debt.id,
            amount_minor=12_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()),
        )
        status = debts_service.debt_status(session, debt, on=TODAY)
        assert status["remaining_minor"] == -2_000

    def test_overdue_detection(self, session: Session) -> None:
        debt = self._debt(session, due_date=TODAY - timedelta(days=1))
        status = debts_service.debt_status(session, debt, on=TODAY)
        assert status["overdue"] is True
        payload = debts_service.overview(session, on=TODAY)
        assert payload["summary"]["overdue_count"] == 1

    def test_lend_creates_asset(self, session: Session) -> None:
        """借出去的钱必须以**应收资产**出现，否则用户会问"我少了 5000，净值怎么没动"。

        注意：净值会因此**增加** 3000 —— 因为在这条债务被记下来之前，
        这笔钱完全不在账上。应收账户代表"别人欠我这笔钱"，
        它是一笔真实的资产，而不是凭空多出来的。
        """
        before = accounts_service.overview(session)["net_worth_minor"]
        debts_service.create_debt(
            session,
            name="借给小李",
            kind=DebtKind.LEND.value,
            counterparty="小李",
            principal_minor=300_000,
            start_date=TODAY,
            create_mirror_account=True,
        )
        after = accounts_service.overview(session)["net_worth_minor"]
        assert after == before + 300_000, "应收资产进入净值"
        created = [
            item for item in accounts_service.overview(session)["accounts"] if item["type"] == "receivable"
        ]
        assert created and created[0]["balance_minor"] == 300_000

    def test_borrow_creates_liability(self, session: Session) -> None:
        """借进来的钱是负债，净值必须减少。"""
        before = accounts_service.overview(session)["net_worth_minor"]
        debts_service.create_debt(
            session,
            name="向小张借",
            kind=DebtKind.BORROW.value,
            counterparty="小张",
            principal_minor=200_000,
            start_date=TODAY,
            create_mirror_account=True,
        )
        after = accounts_service.overview(session)["net_worth_minor"]
        assert after == before - 200_000, "应付负债减少净值"

    def test_overview_summary(self, session: Session) -> None:
        self._debt(session)
        self._debt(session, name="欠小张", kind=DebtKind.BORROW.value, principal_minor=200_000)
        payload = debts_service.overview(session, on=TODAY)
        assert payload["summary"]["receivable_minor"] == 500_000
        assert payload["summary"]["payable_minor"] == 200_000
        assert payload["summary"]["net_minor"] == 300_000
        assert payload["has_debt"] is True

    def test_zero_data_overview(self, session: Session) -> None:
        payload = debts_service.overview(session, on=TODAY)
        assert payload["items"] == []
        assert payload["summary"]["net_minor"] == 0
        assert payload["has_debt"] is False


# =============================================================================
# K 线
# =============================================================================
class TestKline:
    def test_indicators_are_none_until_enough_samples(self) -> None:
        """样本不足给 None，而不是缩短窗口的均值 —— 后者在图上完全看不出来。"""
        values = kline_service.moving_average([1, 2, 3, 4, 5], 3)
        assert values[:2] == [None, None]
        assert values[2:] == [2.0, 3.0, 4.0]

    def test_rsi_neutral_when_flat(self) -> None:
        """ "完全没变化"既不是超买也不是超卖，编一个极值会误导。"""
        assert kline_service.rsi([5.0] * 20, 14)[-1] == 50.0
        assert kline_service.rsi([float(i) for i in range(1, 30)], 14)[-1] == 100.0
        assert kline_service.rsi([float(30 - i) for i in range(1, 30)], 14)[-1] == 0.0

    def test_drawdown_uses_running_peak(self) -> None:
        """用历史最高点而不是"上一根"，否则小回调也会被算成回撤。"""
        assert kline_service.drawdown([10, 12, 9, 15]) == [0.0, 0.0, pytest.approx(-0.25), 0.0]

    def test_ohlc_open_is_previous_close(self, session: Session) -> None:
        """open 取周期开始**前一天**的日终净值 —— 否则"周一亏了钱"在周线上会消失。"""
        account = _account(session)
        accounts_service.update_account(session, account.id, initial_balance_minor=100_000)
        # 上周三花 1000，本周三花 2000（周二/周三属于同一周）
        last_week = TODAY - timedelta(days=TODAY.weekday()) - timedelta(days=5)
        _spend(session, day=last_week, amount=1_000)

        payload = kline_service.series(session, period="week", start=last_week, end=TODAY, indicators=False)
        assert payload["count"] >= 2
        first = payload["bars"][0]
        # 第一根的 open 是它开始前一天的净值（还没花钱 → 100000）
        assert first["open_minor"] == 100_000
        assert first["close_minor"] == 99_000

    def test_ohlc_high_low_span_the_period(self, session: Session) -> None:
        """用**已经过去的完整月份**：当月 K 线只统计到今天，
        落在未来的流水根本不该被算进去（这一点本身也要靠这个测试守住）。
        """
        account = _account(session)
        accounts_service.update_account(session, account.id, initial_balance_minor=100_000)
        previous_month = period_bounds("month", TODAY.replace(day=1) - timedelta(days=1))[0]
        _spend(session, day=previous_month, amount=10_000)
        _spend(session, day=previous_month + timedelta(days=1), amount=30_000)
        _spend(session, day=previous_month + timedelta(days=2), amount=5_000)

        payload = kline_service.series(
            session, period="month", start=previous_month, end=TODAY, indicators=False
        )
        bar = payload["bars"][0]
        assert bar["high_minor"] == 100_000, "最高点是周期开始时的余额"
        assert bar["low_minor"] == 55_000, "最低点是花掉 4.5 万之后"
        assert bar["close_minor"] == 55_000

    def test_volume_is_money_moved(self, session: Session) -> None:
        """成交量是"这个周期里有多少钱动过"，不是余额。"""
        previous_month = period_bounds("month", TODAY.replace(day=1) - timedelta(days=1))[0]
        _spend(session, day=previous_month, amount=10_000)
        _spend(session, day=previous_month + timedelta(days=1), amount=20_000)
        payload = kline_service.series(
            session, period="month", start=previous_month, end=TODAY, indicators=False
        )
        assert payload["bars"][0]["volume_minor"] == 30_000
        assert payload["bars"][0]["tx_count"] == 2

    def test_warmup_makes_first_bar_indicators_valid(self, session: Session) -> None:
        """**这是本模块最容易错的地方**：指标在区间第一根上必须已经收敛。

        不预热的话，MA60 在区间第一根只是"前 1 个样本的均值"，
        图上看起来是一条连续的线，实际上前 59 根都是错的。
        """
        account = _account(session)
        accounts_service.update_account(session, account.id, initial_balance_minor=1_000_000)
        # 造 120 天历史（每天花不同的钱，让净值序列有起伏）
        start = TODAY - timedelta(days=120)
        for offset in range(121):
            day = start + timedelta(days=offset)
            _spend(session, day=day, amount=100 + (offset % 7) * 50)

        payload = kline_service.series(
            session, period="day", start=TODAY - timedelta(days=5), end=TODAY, indicators=True
        )
        assert payload["count"] == 6
        first = payload["bars"][0]
        assert first["ma"]["60"] is not None, "预热后区间第一根的 MA60 必须是真实值"
        assert first["ma"]["20"] is not None
        assert first["rsi"] is not None
        assert first["dif"] is not None
        assert payload["warmup_bars"] == kline_service.WARMUP_BARS

        # 与"不预热"的同一根比较：必须不同，否则说明预热根本没起作用
        cold = kline_service.series(
            session,
            period="day",
            start=TODAY - timedelta(days=5),
            end=TODAY,
            indicators=True,
            warmup=0,
        )
        assert cold["bars"][0]["ma"]["60"] != first["ma"]["60"]

    def test_cache_does_not_serve_stale_values(self, session: Session) -> None:
        """记一笔账之后，K 线的最后一根必须跟着变。

        这里曾经有一个真实的 bug：``bars()`` 只在"缺行"时才重建，
        于是"缓存行还在、底下的日结已经脏了"这种情况会直接返回旧值 ——
        用户记完一笔账，K 线纹丝不动。断言可观察行为（读数变了），
        而不是内部有没有删行，才能守住它。
        """
        month_start = TODAY.replace(day=1)
        # 先记一笔：账本里一笔都没有时，K 线本来就该是空的（见空数据用例）
        _spend(session, day=month_start, amount=1_000)
        first = kline_service.series(session, period="month", start=month_start, end=TODAY, indicators=False)
        close_before = first["bars"][0]["close_minor"]
        assert session.query(AssetOhlc).count() == 1, "第一次读取应写入缓存"

        _spend(session, day=TODAY, amount=7_000)

        second = kline_service.series(session, period="month", start=month_start, end=TODAY, indicators=False)
        assert second["bars"][0]["close_minor"] == close_before - 7_000
        # 高/低点也要跟着更新，而不只是收盘价
        assert second["bars"][0]["low_minor"] == close_before - 7_000

    def test_periods_differ_but_close_agrees(self, session: Session) -> None:
        """不同周期的最后一根 close 必须一致 —— 都表示"现在的净值"。"""
        account = _account(session)
        accounts_service.update_account(session, account.id, initial_balance_minor=50_000)
        _spend(session, day=TODAY, amount=1_000)
        start = TODAY - timedelta(days=20)
        closes = {
            period: kline_service.series(session, period=period, start=start, end=TODAY, indicators=False)[
                "bars"
            ][-1]["close_minor"]
            for period in ("day", "week", "month")
        }
        assert len(set(closes.values())) == 1, f"各周期收口不一致：{closes}"

    def test_empty_range_returns_empty_bars(self, session: Session) -> None:
        """数据为零时给空数组，而不是报错或编一根 0。"""
        payload = kline_service.series(
            session, period="day", start=date(2020, 1, 1), end=date(2020, 1, 5), indicators=False
        )
        assert payload["bars"] == []
        assert payload["count"] == 0

    def test_invalid_period_rejected(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            kline_service.series(session, period="minute", start=TODAY, end=TODAY)


class TestCategoryTrend:
    def test_months_are_continuous(self, session: Session) -> None:
        """没有支出的月份也要出现 —— 堆叠面积图缺一个月会让横轴断开。"""
        _spend(session, day=TODAY, amount=1_000)
        payload = stats_service.category_trend(session, months=3, end=TODAY)
        assert len(payload["months"]) == 3, f"月份轴应连续：{payload['months']}"
        # 只有本月有支出，因此 rows 只覆盖一个月（不伪造 0 行）
        assert {row["month"] for row in payload["rows"]} == {TODAY.isoformat()[:7]}

    def test_splits_preferred(self, session: Session) -> None:
        """分类构成必须遵守"分账优先"，与预算、日历同一口径。"""
        account = _account(session)
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=10_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=12),
            splits=[
                {"category_id": _category(session, "午餐"), "amount_minor": 7_000},
                {"category_id": _category(session, "打车"), "amount_minor": 3_000},
            ],
        )
        rows = stats_service.category_trend(session, months=1, end=TODAY)["rows"]
        by_id = {row["category_id"]: row["amount_minor"] for row in rows}
        assert by_id[_category(session, "午餐")] == 7_000
        assert by_id[_category(session, "打车")] == 3_000

    def test_transfers_excluded(self, session: Session) -> None:
        accounts = accounts_service.list_accounts(session)
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=accounts[0].id,
            to_account_id=accounts[1].id,
            amount_minor=50_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=12),
        )
        assert stats_service.category_trend(session, months=1, end=TODAY)["rows"] == []

    def test_zero_data_returns_empty(self, session: Session) -> None:
        """没有任何流水时返回空数组，而不是报错或编零行。"""
        payload = stats_service.category_trend(session, months=6, end=TODAY)
        assert payload["rows"] == []
        assert len(payload["months"]) == 6, "没有数据时月份轴仍然完整"

    def test_bounds_are_rejected(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            stats_service.category_trend(session, months=0)
        with pytest.raises(ValidationError):
            stats_service.category_trend(session, months=100)
