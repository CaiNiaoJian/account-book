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
from accountbook.services import assets as assets_service
from accountbook.services import attachments as attachments_service
from accountbook.services import budgets as budgets_service
from accountbook.services import categories as categories_service
from accountbook.services import daily as daily_service
from accountbook.services import debts as debts_service
from accountbook.services import kline as kline_service
from accountbook.services import recurring as recurring_service
from accountbook.services import stats as stats_service
from accountbook.services import taxonomy as taxonomy_service
from accountbook.services import transactions as transactions_service
from accountbook.services import trash as trash_service
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


class TestRepaymentPlan:
    def _debt(self, session: Session, **overrides):
        payload = {
            "name": "装修借款",
            "kind": DebtKind.BORROW.value,
            "counterparty": "表哥",
            "principal_minor": 1_200_000,
            "start_date": date(2026, 1, 1),
            "due_date": date(2027, 1, 1),
            "annual_rate_bps": 480,
            "repayment_method": "equal_installment",
            "installments": 12,
        }
        payload.update(overrides)
        return debts_service.create_debt(session, **payload)

    def test_principal_sums_exactly(self, session: Session) -> None:
        """**本金分摊之和必须严格等于原始本金。**

        逐期独立四舍五入会让总额差几毛钱，而"还完最后一期还剩 3 分钱"
        是用户绝对无法接受的。做法是最后一期吸收全部误差。
        """
        for method in ("equal_installment", "equal_principal", "interest_first"):
            debt = self._debt(session, repayment_method=method, name=f"测试-{method}")
            plan = debts_service.repayment_plan(session, debt.id)
            total = sum(row["principal_minor"] for row in plan["rows"])
            assert total == debt.principal_minor, f"{method} 本金之和 {total} != {debt.principal_minor}"
            assert plan["rows"][-1]["balance_minor"] == 0, f"{method} 最后一期余额应为 0"

    def test_principal_sums_exactly_with_awkward_amounts(self, session: Session) -> None:
        """刻意用除不尽的金额与期数：1000001 分 / 7 期。"""
        for method in ("equal_installment", "equal_principal", "interest_first"):
            debt = self._debt(
                session,
                principal_minor=1_000_001,
                installments=7,
                repayment_method=method,
                name=f"除不尽-{method}",
            )
            plan = debts_service.repayment_plan(session, debt.id)
            assert len(plan["rows"]) == 7
            assert sum(row["principal_minor"] for row in plan["rows"]) == 1_000_001
            assert plan["rows"][-1]["balance_minor"] == 0

    def test_zero_rate_does_not_divide_by_zero(self, session: Session) -> None:
        """利率为 0 时等额本息的公式会退化，必须单独走 P/n。"""
        debt = self._debt(session, annual_rate_bps=0)
        plan = debts_service.repayment_plan(session, debt.id)
        assert plan["total_interest_minor"] == 0
        assert sum(row["principal_minor"] for row in plan["rows"]) == debt.principal_minor

    def test_equal_principal_decreases_interest(self, session: Session) -> None:
        """等额本金：每期本金相同、利息逐期递减。"""
        debt = self._debt(session, repayment_method="equal_principal")
        plan = debts_service.repayment_plan(session, debt.id)
        principals = {row["principal_minor"] for row in plan["rows"][:-1]}
        assert len(principals) <= 2, f"各期本金应基本相同：{principals}"
        interests = [row["interest_minor"] for row in plan["rows"]]
        assert interests == sorted(interests, reverse=True), "利息应逐期递减"
        assert interests[-1] < interests[0]

    def test_interest_first_defers_principal(self, session: Session) -> None:
        """先息后本：前面只还利息，最后一期才还本金。"""
        debt = self._debt(session, repayment_method="interest_first", installments=6)
        plan = debts_service.repayment_plan(session, debt.id)
        for row in plan["rows"][:-1]:
            assert row["principal_minor"] == 0
            assert row["interest_minor"] > 0
        assert plan["rows"][-1]["principal_minor"] == debt.principal_minor

    def test_lump_sum_single_row(self, session: Session) -> None:
        debt = self._debt(session, repayment_method="lump_sum", installments=12)
        plan = debts_service.repayment_plan(session, debt.id)
        assert len(plan["rows"]) == 1
        assert plan["rows"][0]["principal_minor"] == debt.principal_minor

    def test_payments_mark_periods_settled(self, session: Session) -> None:
        """实际还款按**累计已还本金**对到期次上，而不是强匹配"每期必须还多少"。"""
        debt = self._debt(session, repayment_method="equal_principal", installments=12)
        per = debt.principal_minor // 12
        for index in range(3):
            debts_service.add_payment(
                session,
                debt.id,
                amount_minor=per,
                occurred_at=datetime.combine(date(2026, 1 + index, 1), datetime.min.time()),
            )
        plan = debts_service.repayment_plan(session, debt.id)
        settled = [row["period"] for row in plan["rows"] if row["settled"]]
        assert settled == [1, 2, 3], f"应结清前 3 期：{settled}"
        assert plan["settled_periods"] == 3

    def test_zero_principal_has_no_plan(self, session: Session) -> None:
        """零本金给空计划，而不是编一堆 0 行。"""
        debt = self._debt(session, principal_minor=0)
        plan = debts_service.repayment_plan(session, debt.id)
        assert plan["has_plan"] is False
        assert plan["rows"] == []
        assert plan["remaining_minor"] == 0

    def test_plan_is_flagged_as_estimate(self, session: Session) -> None:
        """计划是估算：真实计息方式不在范围内，界面必须写明。"""
        debt = self._debt(session)
        plan = debts_service.repayment_plan(session, debt.id)
        assert plan["is_estimate"] is True
        assert plan["total_payable_minor"] == plan["principal_minor"] + plan["total_interest_minor"]

    def test_due_and_overdue_flags(self, session: Session) -> None:
        debt = self._debt(session, start_date=TODAY - timedelta(days=400))
        plan = debts_service.repayment_plan(session, debt.id, on=TODAY)
        assert any(row["due"] for row in plan["rows"])
        # 一期都没还，因此已到期的那些都是逾期
        assert plan["overdue_rows"] >= 1


class TestTrash:
    def _deleted_transaction(self, session: Session):
        row = _spend(session, day=TODAY, amount=1_000)
        transactions_service.delete_transaction(session, row.id)
        return row

    def test_summary_counts_deleted(self, session: Session) -> None:
        self._deleted_transaction(session)
        counts = {item["entity"]: item["count"] for item in trash_service.summary(session)}
        assert counts["transactions"] == 1
        assert counts["accounts"] == 0
        assert set(counts) == set(trash_service.ENTITY_KEYS), "12 个软删除实体都要出现"

    def test_list_only_shows_deleted(self, session: Session) -> None:
        """未删除的不算"在回收站里"。"""
        _spend(session, day=TODAY, amount=1_000)
        payload = trash_service.list_trash(session, "transactions")
        assert payload["items"] == []
        assert payload["total"] == 0

        self._deleted_transaction(session)
        payload = trash_service.list_trash(session, "transactions")
        assert payload["total"] == 1
        assert payload["items"][0]["deleted_at"] is not None

    def test_restore_via_domain_service_marks_daily_dirty(self, session: Session) -> None:
        """恢复流水必须走领域服务：它要顺带重算日结。

        直接改 deleted_at 会让日结缓存停在旧值上 —— 用户恢复了一笔账，
        日历却还是没记的样子。
        """
        row = self._deleted_transaction(session)
        trash_service.restore(session, "transactions", row.id)
        session.refresh(row)
        assert row.deleted_at is None
        # 恢复后日历能重新看到这一天 —— 这正是"必须走领域服务"的原因
        cells = daily_service.get_calendar(session, start=TODAY, end=TODAY, metric="expense")
        assert cells[0].expense_minor > 0

    def test_restore_rejects_row_not_in_trash(self, session: Session) -> None:
        row = _spend(session, day=TODAY, amount=1_000)
        with pytest.raises(ConflictError):
            trash_service.restore(session, "transactions", row.id)

    def test_restore_unknown_entity(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            trash_service.restore(session, "unicorns", 1)

    def test_purge_cascades_to_owned_children(self, session: Session) -> None:
        """带分账的流水**可以**被彻底删除，分账一起消失。

        分账是 `cascade="all, delete-orphan"` 的**组成部分**，
        不是外部引用。purge 最初把它当成引用而拒绝删除 —— 那会让
        "清理回收站"对每一笔有分账的流水都失败。
        """
        row = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=_account(session).id,
            amount_minor=10_000,
            occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=12),
            splits=[
                {"category_id": _category(session, "午餐"), "amount_minor": 7_000},
                {"category_id": _category(session, "打车"), "amount_minor": 3_000},
            ],
        )
        # 分账与流水标签都是"组成部分"，不该被当成外部引用
        assert (
            trash_service.blocking_references(session, trash_service.get_entity("transactions"), row.id) == []
        ), "级联子表（transaction_splits / transaction_tags）不该算作外部引用"

        transactions_service.delete_transaction(session, row.id)

        trash_service.purge(session, "transactions", row.id)
        assert session.get(transactions_service.Transaction, row.id) is None

    def test_purge_blocked_by_real_reference(self, session: Session) -> None:
        """真正的**外部**引用仍然阻止彻底删除，并说清被什么挡住。

        场景：自建分类 + 一条挂在它上面的预算，两者都进了回收站。
        此时删掉分类会让预算指向一个不存在的目标 —— 之后恢复它就是个坏对象。
        """
        parent = _category(session, "餐饮")
        custom = categories_service.create_category(session, name="下午茶", kind="expense", parent_id=parent)
        budget = budgets_service.create_budget(
            session,
            name="下午茶预算",
            scope="category",
            category_id=custom.id,
            period="monthly",
            amount_minor=10_000,
        )
        budgets_service.delete_budget(session, budget.id)
        # 分类本身没有流水，可以正常软删除
        categories_service.delete_category(session, custom.id)

        with pytest.raises(ConflictError) as info:
            trash_service.purge(session, "categories", custom.id)
        assert "budgets" in str(info.value)

    def test_purge_allows_unreferenced(self, session: Session) -> None:
        row = self._deleted_transaction(session)
        trash_service.purge(session, "transactions", row.id)
        assert session.get(type(row), row.id) is None
        assert trash_service.list_trash(session, "transactions")["total"] == 0

    def test_purge_rejects_row_not_in_trash(self, session: Session) -> None:
        row = _spend(session, day=TODAY, amount=1_000)
        with pytest.raises(ConflictError):
            trash_service.purge(session, "transactions", row.id)


class TestBatchEdit:
    def _three(self, session: Session) -> list[int]:
        return [_spend(session, day=TODAY - timedelta(days=index), amount=1_000).id for index in range(3)]

    def test_batch_category(self, session: Session) -> None:
        ids = self._three(session)
        target = _category(session, "打车")
        report = transactions_service.batch_update(session, ids, category_id=target)
        assert report["count"] == 3
        assert report["skipped"] == []
        for row_id in ids:
            assert session.get(transactions_service.Transaction, row_id).category_id == target

    def test_unset_does_not_touch_field(self, session: Session) -> None:
        """没给的字段保持原样 —— UNSET 与 None 是两件事。"""
        ids = self._three(session)
        original = _category(session, "午餐")
        transactions_service.batch_update(session, ids, project_id=None)
        for row_id in ids:
            assert session.get(transactions_service.Transaction, row_id).category_id == original

    def test_explicit_none_clears_category(self, session: Session) -> None:
        """显式传 None 是"把分类清掉"，这是合法且常见的意图。"""
        ids = self._three(session)
        transactions_service.batch_update(session, ids, category_id=None)
        for row_id in ids:
            assert session.get(transactions_service.Transaction, row_id).category_id is None

    def test_add_tags_merges_without_duplicates(self, session: Session) -> None:
        """追加标签是幂等的：批量操作重复执行不该产生重复关联。"""
        ids = self._three(session)
        tag = taxonomy_service.create_tag(session, name="报销")
        transactions_service.batch_update(session, ids, add_tag_ids=[tag.id])
        report = transactions_service.batch_update(session, ids, add_tag_ids=[tag.id])
        assert report["count"] == 3
        row = session.get(transactions_service.Transaction, ids[0])
        assert [item.id for item in row.tags] == [tag.id]

    def test_single_failure_does_not_roll_back_batch(self, session: Session) -> None:
        """单笔失败不该让整批回滚：用户已经选好了一批，
        因为其中一笔的分类被删掉而全部失败是很糟的体验。"""
        ids = self._three(session)
        missing = 999_999
        report = transactions_service.batch_update(
            session, [*ids, missing], category_id=_category(session, "打车")
        )
        assert report["count"] == 3
        assert report["skipped"] == [{"id": missing, "reason": "not_found"}]

    def test_empty_and_oversized_ids_rejected(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            transactions_service.batch_update(session, [], category_id=1)
        with pytest.raises(ValidationError):
            transactions_service.batch_update(session, list(range(1, 502)), category_id=1)

    def test_batch_delete_is_recoverable(self, session: Session) -> None:
        ids = self._three(session)
        report = transactions_service.batch_delete(session, ids)
        assert report["count"] == 3
        for row_id in ids:
            assert session.get(transactions_service.Transaction, row_id).deleted_at is not None
        # 可从回收站恢复
        assert trash_service.list_trash(session, "transactions")["total"] == 3

    def test_batch_delete_skips_missing(self, session: Session) -> None:
        ids = self._three(session)
        report = transactions_service.batch_delete(session, [*ids[:1], 999_999])
        assert report["count"] == 1
        assert len(report["skipped"]) == 1


# 只用魔数前缀构造样本：这里要测的是"类型识别"，不需要真实的图片数据。
# 用一个真 PNG 的完整字节反而会让用例里出现一长串无意义的十六进制。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PDF_BYTES = b"%PDF-1.4\n" + b"0" * 64


class TestAttachments:
    def _txn(self, session: Session):
        return _spend(session, day=TODAY, amount=1_000)

    def test_save_png_round_trip(self, session: Session, tmp_path: Path) -> None:
        txn = self._txn(session)
        row = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="发票.png", transaction_id=txn.id
        )
        assert row.mime == "image/png"
        assert row.file_ref.endswith(".png")
        # 相对引用里不能有用户可控片段 —— 这是路径穿越的根本防线
        assert "发票" not in row.file_ref
        assert row.original_name == "发票.png"
        assert attachments_service.serialize(row)["url"] == f"/api/attachments/{row.id}"

        assert attachments_service.read_bytes(tmp_path, row) == PNG_BYTES
        assert len(attachments_service.list_for_transaction(session, txn.id)) == 1

    def test_extension_comes_from_magic_not_from_name(self, session: Session, tmp_path: Path) -> None:
        """改名成 .png 的 PDF 会被识别为 PDF 并存成 .pdf。

        只看扩展名会被 ``evil.png`` 绕过；只看魔数又无法决定扩展名。
        两者都查，并以**魔数**为准。
        """
        txn = self._txn(session)
        row = attachments_service.save(
            session, tmp_path, PDF_BYTES, filename="伪装.png", transaction_id=txn.id
        )
        assert row.mime == "application/pdf"
        assert row.file_ref.endswith(".pdf")

    def test_rejects_unknown_type(self, session: Session, tmp_path: Path) -> None:
        txn = self._txn(session)
        with pytest.raises(ValidationError, match="不支持的文件类型"):
            attachments_service.save(
                session, tmp_path, b"MZ\x90\x00" + b"0" * 32, filename="evil.exe", transaction_id=txn.id
            )

    def test_rejects_svg(self, session: Session, tmp_path: Path) -> None:
        """SVG 是可执行内容（内嵌脚本），在本地 webview 里渲染等于开 XSS 口子。"""
        txn = self._txn(session)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        with pytest.raises(ValidationError, match="不支持的文件类型"):
            attachments_service.save(session, tmp_path, svg, filename="x.svg", transaction_id=txn.id)

    def test_rejects_oversize(self, session: Session, tmp_path: Path) -> None:
        txn = self._txn(session)
        big = PNG_BYTES + b"0" * (attachments_service.MAX_BYTES + 1)
        with pytest.raises(ValidationError, match="上限"):
            attachments_service.save(session, tmp_path, big, filename="big.png", transaction_id=txn.id)

    def test_rejects_empty(self, session: Session, tmp_path: Path) -> None:
        txn = self._txn(session)
        with pytest.raises(ValidationError, match="为空"):
            attachments_service.save(session, tmp_path, b"", filename="x.png", transaction_id=txn.id)

    def test_content_addressing_deduplicates_file(self, session: Session, tmp_path: Path) -> None:
        """同一份内容上传两次只占一份磁盘，但保留两条记录。"""
        first = self._txn(session)
        second = _spend(session, day=TODAY - timedelta(days=1), amount=2_000)
        one = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="a.png", transaction_id=first.id
        )
        two = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="b.png", transaction_id=second.id
        )
        assert one.file_ref == two.file_ref
        assert one.id != two.id

    def test_delete_keeps_file_when_still_referenced(self, session: Session, tmp_path: Path) -> None:
        """删掉一条记录不该把另一条共用的文件也弄丢。"""
        first = self._txn(session)
        second = _spend(session, day=TODAY - timedelta(days=1), amount=2_000)
        one = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="a.png", transaction_id=first.id
        )
        two = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="b.png", transaction_id=second.id
        )
        path = attachments_service.resolve_path(tmp_path, one.file_ref)
        assert path.exists()

        attachments_service.delete(session, tmp_path, one.id)
        assert path.exists(), "仍有一条记录引用它，文件必须保留"
        assert attachments_service.read_bytes(tmp_path, two) == PNG_BYTES

        attachments_service.delete(session, tmp_path, two.id)
        assert not path.exists(), "最后一条记录删除后文件才该消失"

    def test_path_traversal_is_blocked(self, tmp_path: Path) -> None:
        """``file_ref`` 被手工改坏时，读取仍必须被拦在 root 之内。

        写入时路径由我们生成，但导入 / 插件 / 手改 DB 都可能塞进越界值。
        """
        for bad in ("../outside.png", "a/../../outside.png", "a/../../../etc/passwd"):
            with pytest.raises(ValidationError, match="越界"):
                attachments_service.resolve_path(tmp_path, bad)

    def test_missing_file_gives_clear_error(self, session: Session, tmp_path: Path) -> None:
        """数据库有记录但磁盘上没有时，给明确错误而不是让 FileNotFoundError 冒上去。"""
        txn = self._txn(session)
        row = attachments_service.save(session, tmp_path, PNG_BYTES, filename="a.png", transaction_id=txn.id)
        attachments_service.resolve_path(tmp_path, row.file_ref).unlink()
        with pytest.raises(NotFoundError, match="丢失"):
            attachments_service.read_bytes(tmp_path, row)

    def test_unknown_transaction_rejected(self, session: Session, tmp_path: Path) -> None:
        with pytest.raises(NotFoundError):
            attachments_service.save(session, tmp_path, PNG_BYTES, filename="a.png", transaction_id=999_999)

    def test_soft_deleted_transaction_rejected(self, session: Session, tmp_path: Path) -> None:
        """不能给已删除的流水挂附件 —— 它马上会被恢复成一笔"缺附件"的记录。"""
        txn = self._txn(session)
        transactions_service.delete_transaction(session, txn.id)
        with pytest.raises(NotFoundError):
            attachments_service.save(session, tmp_path, PNG_BYTES, filename="a.png", transaction_id=txn.id)

    def test_card_attachment_needs_artwork(self, session: Session, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="卡面"):
            attachments_service.save(session, tmp_path, PNG_BYTES, filename="c.png", kind="card")

    def test_card_attachment_lands_in_cards_folder(self, session: Session, tmp_path: Path) -> None:
        artwork = assets_service.list_card_artworks(session)[0]
        row = attachments_service.save(
            session, tmp_path, PNG_BYTES, filename="c.png", kind="card", card_artwork_id=artwork.id
        )
        assert row.file_ref.startswith("cards/")
        assert len(attachments_service.list_for_artwork(session, artwork.id)) == 1

    def test_zero_data_list_is_empty(self, session: Session) -> None:
        """没有任何附件时返回空数组，而不是报错。"""
        txn = self._txn(session)
        assert attachments_service.list_for_transaction(session, txn.id) == []


class TestDownsampling:
    """长区间降周期（T8）。

    重点是**不抽稀**：抽掉一根蜡烛，那个周期的开高低收就永久丢失了，
    画出来的"低点"可能比真实的低点高 —— 用户会据此判断"那天没跌那么狠"。
    换更粗的周期则每一根都是真实的聚合结果。
    """

    def test_short_range_keeps_period(self) -> None:
        period, downsampled = kline_service.resolve_period("day", date(2026, 7, 1), date(2026, 10, 1))
        assert period == "day"
        assert downsampled is False

    def test_long_range_coarsens_step_by_step(self) -> None:
        period, downsampled = kline_service.resolve_period("day", date(2021, 1, 1), date(2026, 1, 1))
        assert period == "week", "5 年日线（1827 根）应降到周线"
        assert downsampled is True

    def test_threshold_is_bar_count_not_day_count(self) -> None:
        """判据是**根数**：400 根压线保持日线，401 根才降级。

        用"多少天"当判据会在不同周期上给出不一致的结果 ——
        400 天在日线上是 400 根，在周线上只有 58 根。
        """
        start = date(2026, 1, 1)
        assert kline_service.resolve_period("day", start, start + timedelta(days=399))[0] == "day"
        assert kline_service.resolve_period("day", start, start + timedelta(days=400))[0] == "week"

    def test_year_never_coarsens_further(self) -> None:
        period, downsampled = kline_service.resolve_period("year", date(1900, 1, 1), date(2026, 1, 1))
        assert period == "year"
        assert downsampled is False

    def test_series_reports_requested_and_actual(self, session: Session) -> None:
        """响应里必须**同时**给出用户要的周期与实际用的周期。

        只给一个 period 的话，前端无法区分"本来就要月线"与"被降级了"，
        也就无法给出那句必要的提示。
        """
        account = _account(session)
        accounts_service.update_account(session, account.id, initial_balance_minor=100_000)
        _spend(session, day=TODAY, amount=1_000)

        payload = kline_service.series(
            session, period="day", start=date(2021, 1, 1), end=TODAY, indicators=False
        )
        assert payload["requested_period"] == "day"
        assert payload["period"] == "week"
        assert payload["downsampled"] is True
        assert payload["max_bars"] == kline_service.MAX_BARS

    def test_response_never_exceeds_max_bars_when_coarsenable(self, session: Session) -> None:
        payload = kline_service.series(
            session, period="day", start=date(2019, 1, 1), end=TODAY, indicators=False
        )
        # 允许一年一档时略微超出（年线无法再降级），但日/周/月三档必须收敛
        if payload["period"] != "year":
            assert payload["count"] <= kline_service.MAX_BARS
