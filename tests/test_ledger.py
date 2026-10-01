"""台账（P5）的用例。

这个文件的核心只有一句话：**期初 + 逐笔 = 期末，且期末 = 聚合余额**。
其余用例都在为这句话构造各种容易出错的情形：转账的两个方向、作废流水、
软删除、同秒多笔、跨区间查询、以及调整分录。

台账的价值全在"这本账对不对得上"。一个算得漂漂亮亮但对不上的台账，
比没有台账更糟 —— 用户会拿它当依据。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from accountbook.core.domain import TransactionType
from accountbook.core.errors import ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import categories as categories_service
from accountbook.services import ledger as ledger_service
from accountbook.services import transactions as transactions_service

TODAY = date.today()
MONTH_START = TODAY.replace(day=1)
#: 用例窗口。刻意不用月初：今天是 1 号时 `MONTH_START + 2` 会落到未来，
#: 而未来日期的流水不属于「截至今天」的台账 —— 那会让用例莫名其妙地失败。
#: 改用相对今天的固定偏移，用例在任何一天跑都成立。
WINDOW_START = TODAY - timedelta(days=20)
DAY_1 = TODAY - timedelta(days=18)
DAY_2 = TODAY - timedelta(days=12)
DAY_3 = TODAY - timedelta(days=5)


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


def _accounts(session):
    return accounts_service.list_accounts(session)


def _account(session, index: int = 0):
    return _accounts(session)[index]


def _category(session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    return categories_service.create_category(session, name=name, kind=kind).id


def _at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def _spend(session, *, day: date, amount: int, account=None, hour: int = 12):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=(account or _account(session)).id,
        amount_minor=amount,
        occurred_at=_at(day, hour),
        category_id=_category(session, "午餐"),
    )


def _income(session, *, day: date, amount: int, account=None, hour: int = 9):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.INCOME.value,
        account_id=(account or _account(session)).id,
        amount_minor=amount,
        occurred_at=_at(day, hour),
    )


def _transfer(session, *, day: date, amount: int, source=None, target=None, hour: int = 10):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.TRANSFER.value,
        account_id=(source or _account(session, 0)).id,
        to_account_id=(target or _account(session, 1)).id,
        amount_minor=amount,
        occurred_at=_at(day, hour),
    )


def _first_day_of_last_month(today: date = TODAY) -> date:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


# -----------------------------------------------------------------------------
# 基本结构
# -----------------------------------------------------------------------------
class TestLedgerBasics:
    def test_zero_data_ledger(self, session) -> None:
        """零数据：期初 = 账户起点余额，期末 = 期初，没有明细。

        刻意**不**因为"没有流水"就报错或返回空对象 ——
        一本没有交易的台账是完全正常的状态。
        """
        account = _account(session)
        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        assert document["entries"] == []
        assert document["count"] == 0
        assert document["opening_balance_minor"] == account.initial_balance_minor
        assert document["closing_balance_minor"] == account.initial_balance_minor
        assert document["inflow_minor"] == 0
        assert document["outflow_minor"] == 0
        assert document["check"]["balanced"] is True

    def test_opening_includes_earlier_transactions(self, session) -> None:
        """期初余额必须包含起始日**之前**的流水，否则整本台账都会偏。"""
        account = _account(session)
        _income(session, day=_first_day_of_last_month() + timedelta(days=5), amount=50_000)
        _spend(session, day=_first_day_of_last_month() + timedelta(days=8), amount=10_000)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        assert document["opening_balance_minor"] == account.initial_balance_minor + 40_000
        assert document["entries"] == []

    def test_running_balance_is_monotonic_chain(self, session) -> None:
        """每一行的滚动余额 = 上一行的滚动余额 + 本行金额。"""
        account = _account(session)
        _income(session, day=DAY_1, amount=100_000)
        _spend(session, day=DAY_2, amount=30_000)
        _spend(session, day=DAY_3, amount=20_000)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        running = document["opening_balance_minor"]
        for entry in document["entries"]:
            running += entry["signed_minor"]
            assert entry["running_balance_minor"] == running
        assert document["closing_balance_minor"] == running
        assert document["inflow_minor"] == 100_000
        assert document["outflow_minor"] == 50_000

    def test_same_timestamp_order_is_deterministic(self, session) -> None:
        """同一天同一小时记两笔时，顺序必须由 id 兜底。

        只按 `occurred_at` 排序时两行的先后取决于数据库返回顺序，
        于是滚动余额会时对时错、"期初 + 逐笔 = 期末"也会偶发失败。
        """
        account = _account(session)
        first = _spend(session, day=DAY_1, amount=1_000, hour=12)
        second = _spend(session, day=DAY_1, amount=2_000, hour=12)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        ids = [entry["transaction_id"] for entry in document["entries"]]
        assert ids == [first.id, second.id]
        assert document["check"]["balanced"] is True

    def test_end_before_start_is_rejected(self, session) -> None:
        with pytest.raises(ValidationError, match="早于"):
            ledger_service.build_ledger(
                session, _account(session).id, start=TODAY, end=TODAY - timedelta(days=1)
            )


# -----------------------------------------------------------------------------
# 转账的两个方向
# -----------------------------------------------------------------------------
class TestTransferLegs:
    def test_outgoing_leg_shows_counterparty(self, session) -> None:
        """转出方看到的是"钱去了哪个账户"。"""
        source, target = _account(session, 0), _account(session, 1)
        _transfer(session, day=DAY_1, amount=30_000)

        document = ledger_service.build_ledger(session, source.id, start=WINDOW_START, end=TODAY)
        entry = document["entries"][0]
        assert entry["signed_minor"] == -30_000
        assert entry["leg"] == "self"
        assert entry["counterparty"] == target.name

    def test_incoming_leg_is_included_and_positive(self, session) -> None:
        """转入方**也要**看得到这笔钱 —— 漏掉转入腿会让余额凭空少一截。"""
        source, target = _account(session, 0), _account(session, 1)
        _transfer(session, day=DAY_1, amount=30_000)

        document = ledger_service.build_ledger(session, target.id, start=WINDOW_START, end=TODAY)
        entry = document["entries"][0]
        assert entry["signed_minor"] == 30_000
        assert entry["leg"] == "incoming"
        assert entry["counterparty"] == source.name
        assert document["closing_balance_minor"] == (target.initial_balance_minor + 30_000)

    def test_transfer_does_not_change_global_net(self, session) -> None:
        """转账对全局净额没有影响：转出 −A、转入 +A，合计 0。"""
        before = sum(accounts_service.account_balance(session, item.id) for item in _accounts(session))
        _transfer(session, day=DAY_1, amount=30_000)
        after = sum(accounts_service.account_balance(session, item.id) for item in _accounts(session))
        assert after == before


# -----------------------------------------------------------------------------
# 被排除的行
# -----------------------------------------------------------------------------
class TestExclusions:
    def _void(self, session, row) -> None:
        row.status = "void"
        session.flush()

    def test_void_is_excluded_and_reported(self, session) -> None:
        """作废的流水不计入余额，但**笔数要报出来**。

        用户记了 10 笔而台账只有 8 笔时必须能知道那 2 笔去哪了，
        否则他会怀疑软件算错了。
        """
        account = _account(session)
        _income(session, day=DAY_1, amount=50_000)
        voided = _spend(session, day=DAY_1, amount=9_999)
        self._void(session, voided)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        assert document["count"] == 1
        assert document["outflow_minor"] == 0
        assert document["check"]["excluded_void_count"] == 1
        assert document["check"]["balanced"] is True

    def test_soft_deleted_is_excluded(self, session) -> None:
        account = _account(session)
        _income(session, day=DAY_1, amount=50_000)
        removed = _spend(session, day=DAY_1, amount=9_999)
        transactions_service.delete_transaction(session, removed.id)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        assert document["count"] == 1
        assert document["check"]["balanced"] is True


# -----------------------------------------------------------------------------
# 连续性校验
# -----------------------------------------------------------------------------
class TestContinuity:
    def test_closing_equals_aggregate_when_range_covers_today(self, session) -> None:
        """**这是台账最核心的断言。**"""
        account = _account(session)
        _income(session, day=DAY_1, amount=120_000)
        _spend(session, day=DAY_3, amount=45_000)
        _transfer(session, day=DAY_1, amount=20_000, source=account, target=_account(session, 1))

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        check = document["check"]
        assert check["covers_today"] is True
        assert check["internal_ok"] is True
        assert check["aggregate_ok"] is True
        assert check["balanced"] is True
        assert check["difference_minor"] == 0
        assert document["closing_balance_minor"] == accounts_service.account_balance(session, account.id)

    def test_historical_range_does_not_false_alarm(self, session) -> None:
        """查历史区间时期末本来就与当前余额不同 —— 那不是不一致。"""
        account = _account(session)
        last_month = _first_day_of_last_month()
        _income(session, day=last_month, amount=10_000)
        _spend(session, day=DAY_1, amount=5_000)

        document = ledger_service.build_ledger(
            session,
            account.id,
            start=last_month,
            end=last_month + timedelta(days=5),
        )
        check = document["check"]
        assert check["covers_today"] is False
        assert check["aggregate_ok"] is True
        assert check["balanced"] is True
        assert check["difference_minor"] == 0

    def test_opening_reported_in_check(self, session) -> None:
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        check = document["check"]
        assert check["opening_balance_minor"] == document["opening_balance_minor"]
        assert check["recomputed_closing_minor"] == document["closing_balance_minor"]

    def test_every_seeded_account_is_continuous(self, session) -> None:
        """对**每一个**内置账户都成立，而不只是碰巧第一个。"""
        _income(session, day=DAY_1, amount=50_000)
        _spend(session, day=DAY_1, amount=10_000)
        _transfer(session, day=DAY_1, amount=5_000)
        for account in _accounts(session):
            document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
            assert document["check"]["balanced"] is True, account.name


# -----------------------------------------------------------------------------
# 试算平衡
# -----------------------------------------------------------------------------
class TestTrialBalance:
    def test_zero_data_is_balanced(self, session) -> None:
        result = ledger_service.trial_balance(session)
        assert result["balanced"] is True
        assert result["balance_change_minor"] == 0
        assert result["expected_change_minor"] == 0

    def test_income_and_expense_are_balanced(self, session) -> None:
        _income(session, day=DAY_1, amount=100_000)
        _spend(session, day=DAY_1, amount=30_000)
        result = ledger_service.trial_balance(session)
        assert result["income_minor"] == 100_000
        assert result["expense_minor"] == 30_000
        assert result["balance_change_minor"] == 70_000
        assert result["balanced"] is True

    def test_transfers_are_neutral(self, session) -> None:
        _income(session, day=DAY_1, amount=100_000)
        _transfer(session, day=DAY_1, amount=40_000)
        result = ledger_service.trial_balance(session)
        assert result["balance_change_minor"] == 100_000
        assert result["transfer_neutral_ok"] is True

    def test_adjustments_are_counted(self, session) -> None:
        """**调整分录必须计入这条恒等式。**

        `adjust` 改变余额但不进收支统计 —— 漏掉它会让这个体检
        在每次对账之后都报假警，而假警会让用户再也不看这个体检。
        """
        account = _account(session)
        _income(session, day=DAY_1, amount=100_000)
        ledger_service.reconcile(session, account.id, actual_balance_minor=100_500)

        result = ledger_service.trial_balance(session)
        assert result["adjust_net_minor"] == 500
        assert result["balance_change_minor"] == 100_500
        assert result["expected_change_minor"] == 100_500
        assert result["balanced"] is True

    def test_void_is_excluded_from_trial_balance(self, session) -> None:
        row = _income(session, day=DAY_1, amount=100_000)
        row.status = "void"
        session.flush()
        result = ledger_service.trial_balance(session)
        assert result["income_minor"] == 0
        assert result["balanced"] is True


# -----------------------------------------------------------------------------
# 对账
# -----------------------------------------------------------------------------
class TestReconcile:
    def test_positive_difference_creates_inflow_adjustment(self, session) -> None:
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)

        result = ledger_service.reconcile(session, account.id, actual_balance_minor=10_500)
        assert result["difference_minor"] == 500
        assert result["created_transaction_id"] is not None
        assert result["new_balance_minor"] == 10_500

        row = session.get(transactions_service.Transaction, result["created_transaction_id"])
        assert row.type == TransactionType.ADJUST.value
        assert row.direction == "in"
        assert row.amount_minor == 500

    def test_negative_difference_creates_outflow(self, session) -> None:
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        result = ledger_service.reconcile(session, account.id, actual_balance_minor=9_400)
        row = session.get(transactions_service.Transaction, result["created_transaction_id"])
        assert row.direction == "out"
        assert row.amount_minor == 600
        assert result["new_balance_minor"] == 9_400

    def test_zero_difference_creates_nothing(self, session) -> None:
        """差异为 0 时不建流水：一堆 0 元调整分录会让台账没法看。"""
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        result = ledger_service.reconcile(session, account.id, actual_balance_minor=10_000)
        assert result["created_transaction_id"] is None
        assert result["difference_minor"] == 0

    def test_reconcile_can_preview_only(self, session) -> None:
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        result = ledger_service.reconcile(
            session, account.id, actual_balance_minor=15_000, create_adjustment=False
        )
        assert result["difference_minor"] == 5_000
        assert result["created_transaction_id"] is None
        assert accounts_service.account_balance(session, account.id) == 10_000

    def test_reconcile_keeps_ledger_continuous(self, session) -> None:
        """对账之后台账必须仍然连续，且**拉平到真实余额**。

        这条把三件事串起来验证：调整分录进了台账、进了聚合余额、
        且试算平衡仍然成立。
        """
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        ledger_service.reconcile(session, account.id, actual_balance_minor=10_500)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        assert document["check"]["balanced"] is True
        assert document["closing_balance_minor"] == 10_500
        assert any(entry["type"] == "adjust" for entry in document["entries"])
        assert ledger_service.trial_balance(session)["balanced"] is True

    def test_adjustment_does_not_pollute_income_expense(self, session) -> None:
        """调整分录不该出现在收支统计里 —— 它不是收入也不是支出。"""
        account = _account(session)
        _income(session, day=DAY_1, amount=10_000)
        ledger_service.reconcile(session, account.id, actual_balance_minor=10_500)

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        adjust_entry = next(entry for entry in document["entries"] if entry["type"] == "adjust")
        # 它出现在台账里（改变了余额），但 category_kind 为空、
        # 因此不会进入任何按分类的收支统计
        assert adjust_entry["category_kind"] == ""


class TestFutureDatedRows:
    """未来日期的流水**不属于**截至今天的台账，但必须被报出来。

    这里踩过一个真实的坑：校验最初拿 `account_balance`（**全时段**聚合）
    与台账期末比较，于是只要用户先记了一笔明天的账，校验就报"不一致"。
    一个天天报假警的校验，用户看两次之后就再也不看了 ——
    那比没有校验更糟，因为它把"真的不一致"也一起淹掉了。
    """

    def test_future_row_is_excluded_but_reported(self, session) -> None:
        account = _account(session)
        _income(session, day=TODAY, amount=10_000)
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=3_000,
            occurred_at=_at(TODAY + timedelta(days=3)),
            category_id=_category(session, "午餐"),
        )

        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=TODAY)
        check = document["check"]
        # 那 3000 **不在**本区间台账里
        assert document["outflow_minor"] == 0
        assert check["future_dated_count"] == 1
        assert check["future_dated_net_minor"] == -3_000
        # 但校验仍然成立：比的是"截至今天"的余额
        assert check["balanced"] is True
        assert check["all_time_balance_minor"] == check["aggregate_balance_minor"] - 3_000

    def test_future_row_appears_in_a_window_that_covers_it(self, session) -> None:
        """把区间延到那一天，它就应当出现 —— 否则"看不见"会变成"记丢了"。"""
        account = _account(session)
        future = TODAY + timedelta(days=3)
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=3_000,
            occurred_at=_at(future),
            category_id=_category(session, "午餐"),
        )
        document = ledger_service.build_ledger(session, account.id, start=WINDOW_START, end=future)
        assert document["outflow_minor"] == 3_000
        assert document["check"]["future_dated_count"] == 0
        assert document["check"]["balanced"] is True
