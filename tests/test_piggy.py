"""存钱罐与储蓄目标（P4 / 需求 16）的用例。

自成一个文件：`test_planning.py` 已经装了 7 个互不相关的领域（periods /
recurring / budgets / debts / kline / trash / attachments），
再往里塞存钱罐只会让它更难读。P4 的测试独立成文件，
后续 P5+ 也照此办理。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from accountbook.core.domain import TransactionType
from accountbook.core.errors import ConflictError, NotFoundError, ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import categories as categories_service
from accountbook.services import piggy as piggy_service
from accountbook.services import transactions as transactions_service

TODAY = date.today()


# -----------------------------------------------------------------------------
# 夹具与辅助
# -----------------------------------------------------------------------------
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


def _account(session, index: int = 0):
    return accounts_service.list_accounts(session)[index]


def _category(session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    created = categories_service.create_category(session, name=name, kind=kind)
    return created.id


def _spend(session, *, day: date, amount: int, category: str = "午餐"):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_account(session).id,
        amount_minor=amount,
        occurred_at=datetime.combine(day, datetime.min.time()).replace(hour=12),
        category_id=_category(session, category),
    )


def _income(session, *, day: date, amount: int):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.INCOME.value,
        account_id=_account(session).id,
        amount_minor=amount,
        occurred_at=datetime.combine(day, datetime.min.time()).replace(hour=9),
    )


def _bank(session, *, target: int = 100_000, name: str = "相机基金", **kwargs):
    return piggy_service.create_bank(session, name=name, target_amount_minor=target, **kwargs)


# -----------------------------------------------------------------------------
# 余额与存入
# -----------------------------------------------------------------------------
class TestPiggyBalance:
    def test_balance_is_derived_from_deposits(self, session) -> None:
        bank = _bank(session, target=100_000, initial_minor=20_000)
        assert piggy_service.balance(session, bank.id) == 20_000
        piggy_service.add_deposit(session, bank.id, amount_minor=30_000)
        assert piggy_service.balance(session, bank.id) == 50_000

    def test_zero_data_bank_has_zero_balance(self, session) -> None:
        """零数据：新建的罐子余额是 0、进度是 0、没有任何存入记录。"""
        bank = _bank(session)
        assert piggy_service.balance(session, bank.id) == 0
        assert piggy_service.list_deposits(session, bank.id) == []
        detail = piggy_service.bank_detail(session, bank.id)
        assert detail["ratio"] == 0
        assert detail["milestones"] == []
        assert detail["eta"]["achieved"] is False
        assert detail["eta"]["linear"] is None
        assert detail["eta"]["weighted"] is None

    def test_zero_data_lists_are_empty(self, session) -> None:
        assert piggy_service.list_banks(session) == []
        assert piggy_service.list_goals(session) == []
        assert piggy_service.due_rules(session) == []

    def test_withdraw_more_than_balance_is_refused(self, session) -> None:
        """罐子里有多少才能拿多少 —— 负余额会让液面动画与百分比失去意义。"""
        bank = _bank(session, initial_minor=5_000)
        with pytest.raises(ConflictError) as info:
            piggy_service.add_deposit(session, bank.id, amount_minor=-5_001)
        assert info.value.details["balance_minor"] == 5_000

    def test_withdraw_exactly_balance_is_allowed(self, session) -> None:
        bank = _bank(session, initial_minor=5_000)
        piggy_service.add_deposit(session, bank.id, amount_minor=-5_000, kind="withdraw")
        assert piggy_service.balance(session, bank.id) == 0

    def test_zero_amount_refused(self, session) -> None:
        bank = _bank(session)
        with pytest.raises(ValidationError, match="不能为 0"):
            piggy_service.add_deposit(session, bank.id, amount_minor=0)

    def test_delete_deposit_that_would_go_negative_is_refused(self, session) -> None:
        """删掉一笔**存入**会让余额变小，变成负数就必须拦住。

        这里刻意先存 100、再取走 80（余额 20），然后删掉那笔 100 的存入 ——
        删完余额会是 −80。最初的守卫写成"只拦取出记录"，方向正好反了；
        这条用例就是为了钉住那个错误。
        """
        bank = _bank(session)
        deposit = piggy_service.add_deposit(session, bank.id, amount_minor=10_000)
        piggy_service.add_deposit(session, bank.id, amount_minor=-8_000, kind="withdraw")
        assert piggy_service.balance(session, bank.id) == 2_000

        with pytest.raises(ConflictError, match="负数"):
            piggy_service.delete_deposit(session, deposit.id)

    def test_delete_withdraw_is_allowed(self, session) -> None:
        """删掉一笔取出只是把余额加回去，不该被拦。"""
        bank = _bank(session, initial_minor=10_000)
        withdraw = piggy_service.add_deposit(session, bank.id, amount_minor=-8_000, kind="withdraw")
        piggy_service.delete_deposit(session, withdraw.id)
        assert piggy_service.balance(session, bank.id) == 10_000

    def test_milestones(self, session) -> None:
        # 这里只测纯函数，罐子本身不需要留变量
        _bank(session, target=100_000, initial_minor=50_000)
        assert piggy_service.milestones_reached(50_000, 100_000) == [25, 50]
        assert piggy_service.milestones_reached(100_000, 100_000) == [25, 50, 75, 100]
        assert piggy_service.milestones_reached(0, 100_000) == []

    def test_ratio_not_clamped(self, session) -> None:
        """攒超了要能看出来（液面不能"封顶看起来刚好达成"）。"""
        assert piggy_service.progress(150_000, 100_000) == pytest.approx(1.5)

    def test_unknown_bank_raises(self, session) -> None:
        with pytest.raises(NotFoundError):
            piggy_service.get_bank(session, 999_999)


# -----------------------------------------------------------------------------
# 达成与结清
# -----------------------------------------------------------------------------
class TestAchievement:
    def test_reaching_target_marks_achieved(self, session) -> None:
        bank = _bank(session, target=10_000, initial_minor=10_000)
        session.flush()
        assert bank.status == "achieved"
        assert bank.achieved_at is not None

    def test_withdrawing_after_achievement_keeps_status(self, session) -> None:
        """攒到过就是攒到过：取走一部分不该把状态变回"进行中"，
        否则庆祝与历史记录都失去意义。"""
        bank = _bank(session, target=10_000, initial_minor=10_000)
        session.flush()
        piggy_service.add_deposit(session, bank.id, amount_minor=-4_000, kind="withdraw")
        session.flush()
        assert bank.status == "achieved"

    def test_achieve_creates_transaction_and_settles(self, session) -> None:
        bank = _bank(session, target=10_000, initial_minor=12_000, target_name="一台相机")
        account = _account(session)
        result = piggy_service.achieve_bank(session, bank.id, account_id=account.id)

        # 金额用**罐子余额**而不是目标金额：账实相符优先于数字好看
        assert result["settled_minor"] == 12_000
        assert result["remaining_minor"] == 0
        transaction = session.get(transactions_service.Transaction, result["transaction_id"])
        assert transaction.amount_minor == 12_000
        assert transaction.type == TransactionType.EXPENSE.value
        assert transaction.payee == "一台相机"

        # 历史仍可追溯：结清是一笔负数记录，而不是抹掉存入记录
        deposits = piggy_service.list_deposits(session, bank.id)
        assert any(row.kind == "settle" for row in deposits)
        assert piggy_service.balance(session, bank.id) == 0

    def test_achieve_requires_account_when_creating_transaction(self, session) -> None:
        bank = _bank(session, target=10_000, initial_minor=10_000)
        with pytest.raises(ValidationError, match="账户"):
            piggy_service.achieve_bank(session, bank.id)

    def test_achieve_without_transaction_is_allowed(self, session) -> None:
        """钱可能早就花掉了，只是忘了在罐子上记账。"""
        bank = _bank(session, target=10_000, initial_minor=10_000)
        result = piggy_service.achieve_bank(session, bank.id, create_transaction=False)
        assert result["transaction_id"] is None
        assert result["remaining_minor"] == 0

    def test_empty_bank_cannot_be_settled(self, session) -> None:
        bank = _bank(session)
        with pytest.raises(ConflictError, match="空的"):
            piggy_service.achieve_bank(session, bank.id, account_id=_account(session).id)


# -----------------------------------------------------------------------------
# 预计达成日：双口径
# -----------------------------------------------------------------------------
class TestEta:
    def test_linear_uses_full_history(self, session) -> None:
        """攒了 10 天、共 10 万，目标 20 万 → 线性口径还差 10 天。"""
        bank = _bank(session, target=200_000)
        for offset in range(10):
            piggy_service.add_deposit(
                session,
                bank.id,
                amount_minor=10_000,
                occurred_at=datetime.combine(TODAY - timedelta(days=offset), datetime.min.time()).replace(
                    hour=12
                ),
            )
        eta = piggy_service.estimate_completion(session, bank, today=TODAY)
        assert eta["linear"]["days"] == 10
        assert eta["linear"]["eta"] == (TODAY + timedelta(days=10)).isoformat()

    def test_weighted_reacts_to_stopping(self, session) -> None:
        """**这就是为什么要有第二个口径**：攒了 90 天、最近 60 天一分没存。

        线性口径仍然乐观（累计 ÷ 全部天数），而加权口径会把速度拉下来。
        只给线性会让用户以为"照这个速度快到了"。
        """
        bank = _bank(session, target=1_000_000)
        # 90 天前到 60 天前密集存入，之后完全停止
        for offset in range(60, 90):
            piggy_service.add_deposit(
                session,
                bank.id,
                amount_minor=10_000,
                occurred_at=datetime.combine(TODAY - timedelta(days=offset), datetime.min.time()).replace(
                    hour=12
                ),
            )
        eta = piggy_service.estimate_completion(session, bank, today=TODAY)
        assert eta["linear"] is not None
        assert eta["weighted"] is not None
        # 加权速度明显低于历史平均 —— 这就是"最近偷懒了"的信号
        assert eta["weighted"]["rate_per_day_minor"] < eta["linear"]["rate_per_day_minor"]
        assert eta["weighted"]["days"] > eta["linear"]["days"]
        assert eta["divergent"] is True

    def test_no_deposits_yields_no_estimate(self, session) -> None:
        """零数据：不给出"还要 0 天"或"还要 1 天"这种看起来像在催的数字。"""
        bank = _bank(session, target=100_000)
        eta = piggy_service.estimate_completion(session, bank, today=TODAY)
        assert eta["linear"] is None
        assert eta["weighted"] is None
        assert eta["elapsed_days"] == 0
        assert eta["achieved"] is False

    def test_achieved_bank_skips_estimation(self, session) -> None:
        bank = _bank(session, target=10_000, initial_minor=10_000)
        eta = piggy_service.estimate_completion(session, bank, today=TODAY)
        assert eta["achieved"] is True
        assert eta["linear"] is None
        assert eta["remaining_minor"] == 0

    def test_on_track_compares_with_deadline(self, session) -> None:
        # 目标必须**高于**已存入金额，否则罐子一开始就已达成、
        # estimate_completion 会提前返回而不再算期限差距
        bank = _bank(session, target=1_000_000, deadline=TODAY + timedelta(days=5))
        for offset in range(10):
            piggy_service.add_deposit(
                session,
                bank.id,
                amount_minor=10_000,
                occurred_at=datetime.combine(TODAY - timedelta(days=offset), datetime.min.time()).replace(
                    hour=12
                ),
            )
        eta = piggy_service.estimate_completion(session, bank, today=TODAY)
        assert eta["days_to_deadline"] == 5
        # 已存 10 万、目标 100 万 → 还差 90 万，5 天要攒完就是每天 18 万；
        # 而实际速度是每天 1 万，因此**明显不在轨**。
        # "不在轨"正是这个提示存在的意义 —— 一个永远显示"来得及"的提示没有用。
        assert eta["required_per_day_minor"] == pytest.approx(180_000.0)
        assert eta["on_track"] is False


# -----------------------------------------------------------------------------
# 归集规则
# -----------------------------------------------------------------------------
class TestRoundup:
    def test_roundup_to_next_unit(self) -> None:
        assert piggy_service._roundup_amount(3_281, 100) == 19
        assert piggy_service._roundup_amount(3_200, 100) == 0
        assert piggy_service._roundup_amount(1, 100) == 99

    def test_exact_multiple_deposits_nothing(self, session) -> None:
        """正好是整倍数时存 0 —— 存 `unit` 会凭空多一笔，
        而用户看到"买 100 元的东西罐子里多了 1 元"会认为算错了。"""
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        transaction = _spend(session, day=TODAY, amount=10_000)  # 100.00 元
        applied = piggy_service.apply_rules_to_transactions(session, [transaction.id])
        assert applied == []
        assert piggy_service.balance(session, bank.id) == 0

    def test_roundup_only_applies_to_expense(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        income = _income(session, day=TODAY, amount=10_001)
        assert piggy_service.apply_rules_to_transactions(session, [income.id]) == []

    def test_roundup_is_idempotent(self, session) -> None:
        """编辑流水后重算是常见路径；"改个备注就多攒一次钱"
        是那种用户几乎不可能发现、却会持续虚增罐子的 bug。"""
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        transaction = _spend(session, day=TODAY, amount=3_281)  # 32.81 元
        first = piggy_service.apply_rules_to_transactions(session, [transaction.id])
        assert first[0]["amount_minor"] == 19
        assert piggy_service.balance(session, bank.id) == 19

        second = piggy_service.apply_rules_to_transactions(session, [transaction.id])
        assert second == []
        assert piggy_service.balance(session, bank.id) == 19

    def test_income_percent(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="income_percent", percent_bps=500)
        income = _income(session, day=TODAY, amount=1_000_000)
        applied = piggy_service.apply_rules_to_transactions(session, [income.id])
        assert applied[0]["amount_minor"] == 50_000  # 5%

    def test_category_trigger_only_matches_configured_categories(self, session) -> None:
        bank = _bank(session)
        coffee = _category(session, "咖啡")
        piggy_service.upsert_rule(
            session, bank.id, strategy="category_trigger", category_ids=[coffee], fixed_amount_minor=500
        )
        matched = _spend(session, day=TODAY, amount=3_000, category="咖啡")
        unmatched = _spend(session, day=TODAY, amount=3_000, category="打车")
        applied = piggy_service.apply_rules_to_transactions(session, [matched.id, unmatched.id])
        assert len(applied) == 1
        assert applied[0]["transaction_id"] == matched.id
        assert applied[0]["amount_minor"] == 500

    def test_dry_run_does_not_write(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        transaction = _spend(session, day=TODAY, amount=3_281)
        preview = piggy_service.apply_rules_to_transactions(session, [transaction.id], dry_run=True)
        assert preview[0]["amount_minor"] == 19
        assert piggy_service.balance(session, bank.id) == 0

    def test_disabled_rule_is_skipped(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup", enabled=False)
        transaction = _spend(session, day=TODAY, amount=3_281)
        assert piggy_service.apply_rules_to_transactions(session, [transaction.id]) == []

    def test_paused_bank_is_skipped(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        piggy_service.update_bank(session, bank.id, status="paused")
        transaction = _spend(session, day=TODAY, amount=3_281)
        assert piggy_service.apply_rules_to_transactions(session, [transaction.id]) == []

    def test_rule_validation(self, session) -> None:
        bank = _bank(session)
        with pytest.raises(ValidationError, match="定额"):
            piggy_service.upsert_rule(session, bank.id, strategy="daily_fixed")
        with pytest.raises(ValidationError, match="账户"):
            piggy_service.upsert_rule(session, bank.id, strategy="roundup", deduct_from_account=True)
        with pytest.raises(ValidationError, match="百分比"):
            piggy_service.upsert_rule(session, bank.id, strategy="income_percent", percent_bps=10_001)

    def test_one_rule_per_bank(self, session) -> None:
        bank = _bank(session)
        first = piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        second = piggy_service.upsert_rule(session, bank.id, strategy="daily_fixed", fixed_amount_minor=100)
        assert first.id == second.id
        assert second.strategy == "daily_fixed"


class TestTimeDrivenRules:
    def test_daily_fixed_runs_once_per_day(self, session) -> None:
        """幂等靠 `last_run_date`：应用可能一天被开关很多次，
        而"多攒了一笔"用户很难发现。"""
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="daily_fixed", fixed_amount_minor=500)
        assert len(piggy_service.due_rules(session, today=TODAY)) == 1
        applied = piggy_service.run_due_rules(session, today=TODAY)
        assert applied[0]["amount_minor"] == 500
        assert piggy_service.balance(session, bank.id) == 500

        # 同一天再跑：不该重复
        assert piggy_service.due_rules(session, today=TODAY) == []
        assert piggy_service.run_due_rules(session, today=TODAY) == []
        assert piggy_service.balance(session, bank.id) == 500

    def test_daily_fixed_runs_next_day(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="daily_fixed", fixed_amount_minor=500)
        piggy_service.run_due_rules(session, today=TODAY)
        piggy_service.run_due_rules(session, today=TODAY + timedelta(days=1))
        assert piggy_service.balance(session, bank.id) == 1_000

    def test_weekly_fixed_needs_seven_days(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="weekly_fixed", fixed_amount_minor=2_000)
        piggy_service.run_due_rules(session, today=TODAY)
        assert piggy_service.due_rules(session, today=TODAY + timedelta(days=6)) == []
        assert len(piggy_service.due_rules(session, today=TODAY + timedelta(days=7))) == 1

    def test_monthly_surplus_uses_income_minus_expense(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="monthly_surplus")
        _income(session, day=TODAY, amount=1_000_000)
        _spend(session, day=TODAY, amount=400_000)
        applied = piggy_service.run_due_rules(session, today=TODAY)
        assert applied[0]["amount_minor"] == 600_000

    def test_monthly_surplus_skips_negative_month(self, session) -> None:
        """当月赤字时不该"负着存"—— 那会让罐子余额变成负数。"""
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="monthly_surplus")
        _spend(session, day=TODAY, amount=100_000)
        applied = piggy_service.run_due_rules(session, today=TODAY)
        assert applied[0]["amount_minor"] == 0
        assert piggy_service.balance(session, bank.id) == 0

    def test_monthly_surplus_runs_once_per_month(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="monthly_surplus")
        _income(session, day=TODAY, amount=500_000)
        piggy_service.run_due_rules(session, today=TODAY)
        other_day = TODAY.replace(day=min(28, TODAY.day + 1)) if TODAY.day < 28 else TODAY
        if other_day != TODAY:
            assert piggy_service.due_rules(session, today=other_day) == []

    def test_dry_run_preview(self, session) -> None:
        bank = _bank(session)
        piggy_service.upsert_rule(session, bank.id, strategy="daily_fixed", fixed_amount_minor=700)
        preview = piggy_service.run_due_rules(session, today=TODAY, dry_run=True)
        assert preview[0]["amount_minor"] == 700
        assert piggy_service.balance(session, bank.id) == 0


# -----------------------------------------------------------------------------
# 储蓄目标
# -----------------------------------------------------------------------------
class TestGoals:
    def test_zero_data_goal(self, session) -> None:
        goal = piggy_service.add_goal(session, name="首付", target_amount_minor=3_000_000)
        payload = piggy_service.serialize_goal(session, goal)
        assert payload["saved_minor"] == 0
        assert payload["ratio"] == 0
        assert payload["milestones"] == []

    def test_progress_from_linked_account_balance(self, session) -> None:
        """进度来自账户**实时余额**：账户一变，目标进度立刻跟上。"""
        account = _account(session)
        goal = piggy_service.add_goal(
            session, name="首付", target_amount_minor=500_000, account_id=account.id
        )
        _income(session, day=TODAY, amount=200_000)
        assert piggy_service.goal_saved(session, goal) == 200_000
        _spend(session, day=TODAY, amount=50_000)
        assert piggy_service.goal_saved(session, goal) == 150_000

    def test_progress_from_contributions(self, session) -> None:
        goal = piggy_service.add_goal(session, name="旅行", target_amount_minor=100_000)
        piggy_service.contribute(session, goal.id, amount_minor=30_000)
        piggy_service.contribute(session, goal.id, amount_minor=20_000)
        assert piggy_service.goal_saved(session, goal) == 50_000
        assert len(piggy_service.list_contributions(session, goal.id)) == 2

    def test_account_and_contributions_combine(self, session) -> None:
        account = _account(session)
        goal = piggy_service.add_goal(
            session, name="首付", target_amount_minor=500_000, account_id=account.id
        )
        _income(session, day=TODAY, amount=100_000)
        piggy_service.contribute(session, goal.id, amount_minor=50_000)
        assert piggy_service.goal_saved(session, goal) == 150_000

    def test_negative_account_balance_counts_as_is(self, session) -> None:
        """信用卡欠款照实计入而不是夹到 0：夹掉会让"我把首付账户刷爆了"
        看起来跟"一分没存"一样，而前者显然更紧急。"""
        account = _account(session)
        goal = piggy_service.add_goal(
            session, name="首付", target_amount_minor=500_000, account_id=account.id
        )
        _spend(session, day=TODAY, amount=10_000)
        assert piggy_service.goal_saved(session, goal) == -10_000

    def test_goal_achieved_when_target_reached(self, session) -> None:
        goal = piggy_service.add_goal(session, name="旅行", target_amount_minor=50_000)
        piggy_service.contribute(session, goal.id, amount_minor=50_000)
        session.flush()
        assert goal.status == "achieved"
        assert goal.achieved_at is not None

    def test_contribution_over_withdraw_is_refused(self, session) -> None:
        goal = piggy_service.add_goal(session, name="旅行", target_amount_minor=50_000)
        piggy_service.contribute(session, goal.id, amount_minor=10_000)
        with pytest.raises(ConflictError):
            piggy_service.contribute(session, goal.id, amount_minor=-10_001)

    def test_goal_validation(self, session) -> None:
        with pytest.raises(ValidationError, match="名称"):
            piggy_service.add_goal(session, name="  ", target_amount_minor=1_000)
        with pytest.raises(ValidationError, match="大于 0"):
            piggy_service.add_goal(session, name="x", target_amount_minor=0)
        with pytest.raises(NotFoundError):
            piggy_service.add_goal(session, name="x", target_amount_minor=1_000, account_id=999_999)


class TestBankToGoal:
    def test_inject_bank_into_linked_goal(self, session) -> None:
        goal = piggy_service.add_goal(session, name="首付", target_amount_minor=1_000_000)
        bank = _bank(session, target=100_000, initial_minor=80_000, goal_id=goal.id)
        result = piggy_service.inject_bank_into_goal(session, bank.id)
        assert result["injected_minor"] == 80_000
        assert piggy_service.goal_saved(session, goal) == 80_000
        # 罐子被结清，但它的历史还在
        assert piggy_service.balance(session, bank.id) == 0
        assert any(row.kind == "settle" for row in piggy_service.list_deposits(session, bank.id))

    def test_inject_without_goal_is_refused(self, session) -> None:
        bank = _bank(session, initial_minor=1_000)
        with pytest.raises(ValidationError, match="储蓄目标"):
            piggy_service.inject_bank_into_goal(session, bank.id)

    def test_inject_empty_bank_is_refused(self, session) -> None:
        goal = piggy_service.add_goal(session, name="首付", target_amount_minor=100_000)
        bank = _bank(session, goal_id=goal.id)
        with pytest.raises(ConflictError, match="空的"):
            piggy_service.inject_bank_into_goal(session, bank.id)


# -----------------------------------------------------------------------------
# 软删除与列表
# -----------------------------------------------------------------------------
class TestBankLifecycle:
    def test_soft_delete_hides_from_list(self, session) -> None:
        bank = _bank(session)
        piggy_service.delete_bank(session, bank.id)
        assert piggy_service.list_banks(session) == []
        assert len(piggy_service.list_banks(session, include_deleted=True)) == 1
        with pytest.raises(NotFoundError):
            piggy_service.get_bank(session, bank.id)

    def test_restore_recomputes_achievement(self, session) -> None:
        """恢复后重算达成：罐子可能在被删期间"其实已经攒够了"。"""
        bank = _bank(session, target=10_000, initial_minor=10_000)
        session.flush()
        bank.status = "active"  # 人为退回，模拟"删除时还没达成"
        bank.achieved_at = None
        piggy_service.delete_bank(session, bank.id)
        restored = piggy_service.restore_bank(session, bank.id)
        assert restored.status == "achieved"

    def test_list_orders_by_priority(self, session) -> None:
        low = _bank(session, name="低", priority=1)
        high = _bank(session, name="高", priority=9)
        assert (low.priority, high.priority) == (1, 9)
        names = [item.name for item in piggy_service.list_banks(session)]
        assert names.index("高") < names.index("低")

    def test_serialize_includes_rule_and_eta(self, session) -> None:
        bank = _bank(session, target=100_000, initial_minor=50_000)
        piggy_service.upsert_rule(session, bank.id, strategy="roundup")
        payload = piggy_service.serialize_bank(session, bank)
        assert payload["rule"]["strategy"] == "roundup"
        assert payload["milestones"] == [25, 50]
        assert "eta" in payload

    def test_broken_category_json_is_tolerated(self, session) -> None:
        """坏 JSON 当作空列表，而不是让整个页面打不开。"""
        bank = _bank(session)
        rule = piggy_service.upsert_rule(session, bank.id, strategy="category_trigger")
        rule.category_ids = "{ 不是合法 JSON"
        session.flush()
        assert piggy_service.serialize_rule(rule)["category_ids"] == []
