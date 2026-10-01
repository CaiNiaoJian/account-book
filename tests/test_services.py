"""服务层用例 —— 业务规则的直接验证。

这些用例比接口测试更短、更快，因为它们只依赖一个数据库会话。
覆盖重点是**会悄悄算错数字**的规则：

* 转账不能计入收支（否则月支出凭空翻倍）；
* 分账金额之和必须等于流水金额（否则按分类汇总对不上）；
* 分类方向必须匹配流水类型（否则"工资"记成支出）；
* 删除被引用的账户/分类必须被拒绝（否则统计出现空洞）；
* 移动分类要级联更新子树路径（否则报表按大类汇总会漏）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from accountbook.core.domain import (
    AccountType,
    CategoryKind,
    ProjectStatus,
    TransactionStatus,
    TransactionType,
)
from accountbook.core.errors import (
    ConflictError,
    NotFoundError,
    ProtectedEntityError,
    ValidationError,
)
from accountbook.core.money import parse_amount
from accountbook.db.migrations import run_migrations
from accountbook.db.models import Category
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import categories as categories_service
from accountbook.services import stats as stats_service
from accountbook.services import taxonomy as taxonomy_service
from accountbook.services import transactions as transactions_service


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """迁移 + 种子数据齐备的临时账本。"""
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


def _first_account(session: Session, name: str = "现金"):
    return (
        accounts_service.list_accounts(session)[0]
        if name is None
        else next(account for account in accounts_service.list_accounts(session) if account.name == name)
    )


def _category(session: Session, name: str):
    from sqlalchemy import select

    return session.scalars(
        select(Category).where(Category.name == name, Category.deleted_at.is_(None))
    ).first()


# -----------------------------------------------------------------------------
# 账户
# -----------------------------------------------------------------------------
class TestAccountService:
    def test_seed_accounts_exist(self, session: Session) -> None:
        names = [account.name for account in accounts_service.list_accounts(session)]
        assert names[:3] == ["现金", "微信", "支付宝"]

    def test_create_and_duplicate_name_rejected(self, session: Session) -> None:
        accounts_service.create_account(session, name="招行储蓄卡", type=AccountType.DEBIT_CARD.value)
        with pytest.raises(ConflictError, match="同名"):
            accounts_service.create_account(session, name="招行储蓄卡", type=AccountType.DEBIT_CARD.value)

    def test_rejects_unknown_fields(self, session: Session) -> None:
        """白名单之外的字段必须被拒绝 —— 这是越权改字段最经典的入口。"""
        with pytest.raises(ValidationError, match="不支持"):
            accounts_service.create_account(session, name="X", is_admin=True)

    def test_rejects_invalid_bill_day(self, session: Session) -> None:
        with pytest.raises(ValidationError, match="1–28"):
            accounts_service.create_account(session, name="X", bill_day=31)

    def test_balance_reflects_initial_and_transactions(self, session: Session) -> None:
        account = accounts_service.create_account(
            session, name="测试账户", initial_balance_minor=parse_amount("1000")
        )
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("120.50"),
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )
        transactions_service.create_transaction(
            session,
            type=TransactionType.INCOME.value,
            account_id=account.id,
            amount_minor=parse_amount("300"),
            occurred_at=datetime(2026, 3, 2, 12, 0),
        )

        assert accounts_service.account_balance(session, account.id) == parse_amount("1179.50")

    def test_delete_rejected_when_used(self, session: Session) -> None:
        account = _first_account(session, "现金")
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=100,
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )
        with pytest.raises(ConflictError, match="流水"):
            accounts_service.delete_account(session, account.id)

    def test_delete_and_restore_roundtrip(self, session: Session) -> None:
        account = accounts_service.create_account(session, name="临时账户")
        accounts_service.delete_account(session, account.id)
        assert all(item.id != account.id for item in accounts_service.list_accounts(session))

        restored = accounts_service.restore_account(session, account.id)
        assert restored.is_deleted is False

    def test_overview_classifies_by_sign_not_by_type(self, session: Session) -> None:
        """多还款的信用卡会变成正余额，按类型硬分类会算错。"""
        card = accounts_service.create_account(
            session,
            name="信用卡",
            type=AccountType.CREDIT_CARD.value,
            initial_balance_minor=parse_amount("500"),  # 多还了 500
        )
        cash = _first_account(session, "现金")

        overview = accounts_service.overview(session)
        by_id = {item["id"]: item for item in overview["accounts"]}

        assert by_id[card.id]["is_liability_type"] is True
        assert by_id[card.id]["balance_minor"] == parse_amount("500")
        # 正余额计入资产，而不是因为"类型是信用卡"被算成负债
        assert overview["assets_minor"] >= parse_amount("500")
        assert overview["net_worth_minor"] == overview["assets_minor"] - overview["liabilities_minor"]
        assert cash.id in by_id

    def test_archived_accounts_hidden_by_default(self, session: Session) -> None:
        account = accounts_service.create_account(session, name="旧卡", is_archived=True)
        assert all(item.id != account.id for item in accounts_service.list_accounts(session))
        assert any(
            item.id == account.id for item in accounts_service.list_accounts(session, include_archived=True)
        )


# -----------------------------------------------------------------------------
# 分类
# -----------------------------------------------------------------------------
class TestCategoryService:
    def test_seed_tree_is_deep_and_bidirectional(self, session: Session) -> None:
        expense_tree = categories_service.tree(session, kind=CategoryKind.EXPENSE.value)
        income_tree = categories_service.tree(session, kind=CategoryKind.INCOME.value)

        assert len(expense_tree) >= 14
        assert len(income_tree) >= 5
        food = next(node for node in expense_tree if node["name"] == "餐饮")
        assert len(food["children"]) >= 8

    def test_create_child_inherits_kind_and_computes_path(self, session: Session) -> None:
        parent = _category(session, "餐饮")
        child = categories_service.create_category(
            session, name="夜宵", kind=parent.kind, parent_id=parent.id
        )

        assert child.depth == parent.depth + 1
        assert child.path == f"{parent.path}/{child.id}"

    def test_kind_mismatch_rejected(self, session: Session) -> None:
        food = _category(session, "餐饮")
        with pytest.raises(ValidationError, match="方向"):
            categories_service.create_category(
                session, name="错误子类", kind=CategoryKind.INCOME.value, parent_id=food.id
            )

    def test_sibling_duplicate_rejected(self, session: Session) -> None:
        food = _category(session, "餐饮")
        with pytest.raises(ConflictError, match="同名"):
            categories_service.create_category(session, name="早餐", kind=food.kind, parent_id=food.id)

    def test_root_duplicate_rejected(self, session: Session) -> None:
        """根级分类的 parent_id 是 NULL，唯一索引拦不住 —— 必须由服务层校验。"""
        with pytest.raises(ConflictError, match="同名"):
            categories_service.create_category(session, name="餐饮", kind=CategoryKind.EXPENSE.value)

    def test_move_updates_whole_subtree(self, session: Session) -> None:
        """移动父分类后，子树的 path 与 depth 都必须跟着变。"""
        food = _category(session, "餐饮")
        other = _category(session, "其他支出")

        categories_service.move_category(session, food.id, other.id)

        refreshed_food = _category(session, "餐饮")
        refreshed_breakfast = _category(session, "早餐")
        assert refreshed_food.parent_id == other.id
        assert refreshed_food.depth == other.depth + 1
        assert refreshed_breakfast.path.startswith(refreshed_food.path + "/"), "子分类路径未级联更新"
        assert refreshed_breakfast.depth == refreshed_food.depth + 1

    def test_move_into_own_descendant_rejected(self, session: Session) -> None:
        food = _category(session, "餐饮")
        breakfast = _category(session, "早餐")
        with pytest.raises(ValidationError, match="子分类"):
            categories_service.move_category(session, food.id, breakfast.id)

    def test_system_category_cannot_be_deleted(self, session: Session) -> None:
        food = _category(session, "餐饮")
        with pytest.raises(ProtectedEntityError, match="不可删除"):
            categories_service.delete_category(session, food.id)

    def test_custom_category_with_children_cannot_be_deleted(self, session: Session) -> None:
        parent = categories_service.create_category(session, name="自建父类", kind=CategoryKind.EXPENSE.value)
        categories_service.create_category(
            session, name="自建子类", kind=CategoryKind.EXPENSE.value, parent_id=parent.id
        )
        with pytest.raises(ConflictError, match="子分类"):
            categories_service.delete_category(session, parent.id)

    def test_merge_migrates_transactions_and_splits(self, session: Session) -> None:
        """合并必须同时迁移流水与分账 —— 只迁移流水会留下指向已删分类的分账。"""
        account = _first_account(session, "现金")
        source = categories_service.create_category(session, name="吃喝", kind=CategoryKind.EXPENSE.value)
        target = _category(session, "餐饮")

        plain = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            category_id=source.id,
            amount_minor=parse_amount("50"),
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )
        split = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("100"),
            occurred_at=datetime(2026, 3, 2, 12, 0),
            splits=[
                {"category_id": source.id, "amount_minor": parse_amount("60")},
                {"category_id": target.id, "amount_minor": parse_amount("40")},
            ],
        )

        result = categories_service.merge_category(session, source.id, target.id)

        assert result["transactions"] == 1
        assert result["splits"] == 1
        session.refresh(plain)
        session.refresh(split)
        assert plain.category_id == target.id
        assert {item.category_id for item in split.splits} == {target.id}
        assert _category(session, "吃喝") is None

    def test_hidden_category_still_selectable_in_tree(self, session: Session) -> None:
        other = _category(session, "其他支出")
        categories_service.set_hidden(session, other.id, True)
        assert other.is_hidden is True
        tree = categories_service.tree(session, kind=CategoryKind.EXPENSE.value)
        assert any(node["name"] == "其他支出" for node in tree)

    def test_descendant_ids_uses_materialized_path(self, session: Session) -> None:
        food = _category(session, "餐饮")
        ids = categories_service.descendant_ids(session, food.id)
        assert food.id in ids
        assert len(ids) >= 9


# -----------------------------------------------------------------------------
# 流水
# -----------------------------------------------------------------------------
class TestTransactionService:
    def test_expense_defaults(self, session: Session) -> None:
        account = _first_account(session, "现金")
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=3500,
        )
        assert transaction.direction == "out"
        assert transaction.base_amount_minor == 3500
        assert transaction.status == TransactionStatus.CLEARED.value

    def test_direction_must_match_type(self, session: Session) -> None:
        account = _first_account(session, "现金")
        with pytest.raises(ValidationError, match="方向"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.EXPENSE.value,
                direction="in",
                account_id=account.id,
                amount_minor=100,
            )

    def test_amount_must_be_positive_integer(self, session: Session) -> None:
        account = _first_account(session, "现金")
        with pytest.raises(ValidationError, match="整数"):
            transactions_service.create_transaction(
                session, type=TransactionType.EXPENSE.value, account_id=account.id, amount_minor=12.5
            )
        with pytest.raises(ValidationError, match="大于 0"):
            transactions_service.create_transaction(
                session, type=TransactionType.EXPENSE.value, account_id=account.id, amount_minor=0
            )

    def test_transfer_moves_money_between_accounts(self, session: Session) -> None:
        source = _first_account(session, "现金")
        target = _first_account(session, "支付宝")

        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=source.id,
            to_account_id=target.id,
            amount_minor=parse_amount("200"),
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )

        assert accounts_service.account_balance(session, source.id) == -parse_amount("200")
        assert accounts_service.account_balance(session, target.id) == parse_amount("200")

    def test_transfer_requires_distinct_target(self, session: Session) -> None:
        source = _first_account(session, "现金")
        with pytest.raises(ValidationError, match="目标账户"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.TRANSFER.value,
                account_id=source.id,
                amount_minor=100,
            )
        with pytest.raises(ValidationError, match="不能相同"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.TRANSFER.value,
                account_id=source.id,
                to_account_id=source.id,
                amount_minor=100,
            )

    def test_expense_cannot_use_income_category(self, session: Session) -> None:
        """把"工资"记成支出是明显的记账错误，从入口就挡住。"""
        account = _first_account(session, "现金")
        salary = _category(session, "工资")
        with pytest.raises(ValidationError, match="方向不匹配"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.EXPENSE.value,
                account_id=account.id,
                category_id=salary.id,
                amount_minor=100,
            )

    def test_transfer_cannot_carry_category(self, session: Session) -> None:
        source = _first_account(session, "现金")
        target = _first_account(session, "支付宝")
        food = _category(session, "餐饮")
        with pytest.raises(ValidationError, match="不能指定收支分类"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.TRANSFER.value,
                account_id=source.id,
                to_account_id=target.id,
                category_id=food.id,
                amount_minor=100,
            )

    def test_splits_must_sum_to_amount(self, session: Session) -> None:
        account = _first_account(session, "现金")
        food = _category(session, "餐饮")
        other = _category(session, "其他支出")
        with pytest.raises(ValidationError, match="之和"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.EXPENSE.value,
                account_id=account.id,
                amount_minor=parse_amount("100"),
                splits=[
                    {"category_id": food.id, "amount_minor": parse_amount("60")},
                    {"category_id": other.id, "amount_minor": parse_amount("30")},
                ],
            )

    def test_single_split_rejected(self, session: Session) -> None:
        account = _first_account(session, "现金")
        food = _category(session, "餐饮")
        with pytest.raises(ValidationError, match="至少"):
            transactions_service.create_transaction(
                session,
                type=TransactionType.EXPENSE.value,
                account_id=account.id,
                amount_minor=100,
                splits=[{"category_id": food.id, "amount_minor": 100}],
            )

    def test_split_breakdown_prefers_splits_over_main_category(self, session: Session) -> None:
        """分账记录的是钱真正的去向，汇总时必须优先。"""
        account = _first_account(session, "现金")
        food = _category(session, "餐饮")
        other = _category(session, "其他支出")

        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            category_id=other.id,
            amount_minor=parse_amount("100"),
            occurred_at=datetime(2026, 3, 5, 12, 0),
            splits=[
                {"category_id": food.id, "amount_minor": parse_amount("70")},
                {"category_id": other.id, "amount_minor": parse_amount("30")},
            ],
        )

        result = transactions_service.summary(
            session,
            start=datetime(2026, 3, 1),
            end=datetime(2026, 3, 31, 23, 59),
            top_categories=5,
        )
        by_name = {item["category_name"]: item["amount_minor"] for item in result.by_category}
        assert by_name["餐饮"] == parse_amount("70")
        assert by_name["其他支出"] == parse_amount("30")

    def test_summary_excludes_transfers(self, session: Session) -> None:
        """转账不算收支 —— 否则"工资卡转支付宝"会让月支出凭空翻倍。"""
        source = _first_account(session, "现金")
        target = _first_account(session, "支付宝")
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=source.id,
            amount_minor=parse_amount("100"),
            occurred_at=datetime(2026, 3, 5, 12, 0),
        )
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=source.id,
            to_account_id=target.id,
            amount_minor=parse_amount("9999"),
            occurred_at=datetime(2026, 3, 5, 13, 0),
        )

        result = transactions_service.summary(
            session, start=datetime(2026, 3, 1), end=datetime(2026, 3, 31, 23, 59)
        )
        assert result.expense_minor == parse_amount("100")
        assert result.income_minor == 0
        assert result.transaction_count == 1

    def test_void_transactions_excluded_from_balance_and_summary(self, session: Session) -> None:
        account = accounts_service.create_account(session, name="作废测试", initial_balance_minor=0)
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("50"),
            occurred_at=datetime(2026, 3, 5, 12, 0),
        )
        transactions_service.update_transaction(session, transaction.id, status=TransactionStatus.VOID.value)

        assert accounts_service.account_balance(session, account.id) == 0

    def test_delete_and_restore(self, session: Session) -> None:
        account = accounts_service.create_account(session, name="删除测试", initial_balance_minor=0)
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("20"),
            occurred_at=datetime(2026, 3, 5, 12, 0),
        )
        transactions_service.delete_transaction(session, transaction.id)
        assert accounts_service.account_balance(session, account.id) == 0

        transactions_service.restore_transaction(session, transaction.id)
        assert accounts_service.account_balance(session, account.id) == -parse_amount("20")

    def test_list_filters_by_keyword_account_and_category(self, session: Session) -> None:
        account = _first_account(session, "现金")
        food = _category(session, "餐饮")
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            category_id=food.id,
            payee="楼下面馆",
            amount_minor=parse_amount("35"),
            occurred_at=datetime(2026, 3, 5, 12, 0),
        )

        query = transactions_service.TransactionQuery(keyword="面馆")
        items, total = transactions_service.list_transactions(session, query)
        assert total == 1
        assert items[0].payee == "楼下面馆"

        query = transactions_service.TransactionQuery(category_ids=[food.id])
        assert transactions_service.list_transactions(session, query)[1] == 1

        query = transactions_service.TransactionQuery(account_ids=[account.id])
        assert transactions_service.list_transactions(session, query)[1] == 1

        query = transactions_service.TransactionQuery(keyword="不存在的商户")
        assert transactions_service.list_transactions(session, query)[1] == 0

    def test_pagination_reports_true_total(self, session: Session) -> None:
        account = _first_account(session, "现金")
        for index in range(7):
            transactions_service.create_transaction(
                session,
                type=TransactionType.EXPENSE.value,
                account_id=account.id,
                amount_minor=100 + index,
                occurred_at=datetime(2026, 3, 1, 12, 0) + timedelta(days=index),
            )

        query = transactions_service.TransactionQuery(limit=3, offset=0)
        items, total = transactions_service.list_transactions(session, query)
        assert len(items) == 3
        assert total == 7, "总数必须是满足条件的总数，而不是当页条数"

    def test_daily_totals_fill_missing_days(self, session: Session) -> None:
        """没有记账的日子也要占位，否则日历会缺格。"""
        account = _first_account(session, "现金")
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("30"),
            occurred_at=datetime(2026, 3, 3, 12, 0),
        )

        totals = transactions_service.daily_totals(session, start=date(2026, 3, 1), end=date(2026, 3, 5))
        assert len(totals) == 5
        by_day = {item.day: item for item in totals}
        assert by_day[date(2026, 3, 3)].expense_minor == parse_amount("30")
        assert by_day[date(2026, 3, 4)].expense_minor == 0
        assert by_day[date(2026, 3, 4)].transaction_count == 0

    def test_update_can_clear_splits(self, session: Session) -> None:
        account = _first_account(session, "现金")
        food = _category(session, "餐饮")
        other = _category(session, "其他支出")
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("100"),
            splits=[
                {"category_id": food.id, "amount_minor": parse_amount("60")},
                {"category_id": other.id, "amount_minor": parse_amount("40")},
            ],
        )

        transactions_service.update_transaction(session, transaction.id, splits=[])
        session.refresh(transaction)
        assert transaction.splits == []

    def test_unknown_account_rejected(self, session: Session) -> None:
        with pytest.raises(NotFoundError):
            transactions_service.create_transaction(
                session, type=TransactionType.EXPENSE.value, account_id=999999, amount_minor=100
            )


# -----------------------------------------------------------------------------
# 标签 / 项目 / 成员
# -----------------------------------------------------------------------------
class TestTaxonomyService:
    def test_tag_unique_name(self, session: Session) -> None:
        taxonomy_service.create_tag(session, name="出差")
        with pytest.raises(ConflictError):
            taxonomy_service.create_tag(session, name="出差")

    def test_tag_delete_rejected_when_used(self, session: Session) -> None:
        tag = taxonomy_service.create_tag(session, name="报销")
        account = _first_account(session, "现金")
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=100,
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )
        transaction.tags.append(tag)
        session.flush()

        with pytest.raises(ConflictError, match="流水"):
            taxonomy_service.delete_tag(session, tag.id)

    def test_project_lifecycle(self, session: Session) -> None:
        project = taxonomy_service.create_project(session, name="装修", budget_minor=parse_amount("50000"))
        assert project.status == ProjectStatus.ACTIVE.value
        updated = taxonomy_service.update_project(session, project.id, status=ProjectStatus.ARCHIVED.value)
        assert updated.status == ProjectStatus.ARCHIVED.value

    def test_only_one_self_member(self, session: Session) -> None:
        first = taxonomy_service.create_member(session, name="我", is_self=True)
        second = taxonomy_service.create_member(session, name="配偶", is_self=True)

        members = {item.id: item for item in taxonomy_service.list_members(session)}
        assert members[first.id].is_self is False
        assert members[second.id].is_self is True


# -----------------------------------------------------------------------------
# 统计
# -----------------------------------------------------------------------------
class TestStatsService:
    def test_month_bounds_handle_december_and_february(self) -> None:
        start, end = stats_service.month_bounds(date(2026, 12, 15))
        assert start.date() == date(2026, 12, 1)
        assert end.date() == date(2026, 12, 31)

        start, end = stats_service.month_bounds(date(2026, 2, 10))
        assert end.date() == date(2026, 2, 28)

        start, end = stats_service.month_bounds(date(2024, 2, 10))
        assert end.date() == date(2024, 2, 29), "闰年必须有 29 日"

    def test_previous_period_is_equal_length(self) -> None:
        """环比区间必须与本期等长。

        刻意只断言"到秒级相等"：上期右端点取 ``start - 1µs``，
        以免把恰好落在本期起点瞬间的那笔流水同时算进两期。
        这 1 微秒的差异不影响任何业务判断。
        """
        start, end = stats_service.month_bounds(date(2026, 3, 15))
        prev_start, prev_end = stats_service.previous_period(start, end)
        drift = abs(((prev_end - prev_start) - (end - start)).total_seconds())
        assert drift <= 1, f"区间长度偏差过大：{drift} 秒"
        assert prev_end < start, "上期必须完全在本期之前"

    def test_dashboard_shape_and_math(self, session: Session) -> None:
        account = accounts_service.create_account(
            session, name="仪表盘测试", initial_balance_minor=parse_amount("1000")
        )
        today = date.today()
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("120"),
            occurred_at=datetime.combine(today, datetime.min.time()).replace(hour=12),
        )
        transactions_service.create_transaction(
            session,
            type=TransactionType.INCOME.value,
            account_id=account.id,
            amount_minor=parse_amount("500"),
            occurred_at=datetime.combine(today, datetime.min.time()).replace(hour=13),
        )

        payload = stats_service.dashboard(session)

        assert payload["month"]["income_minor"] >= parse_amount("500")
        assert payload["month"]["expense_minor"] >= parse_amount("120")
        assert payload["month"]["net_minor"] == (
            payload["month"]["income_minor"] - payload["month"]["expense_minor"]
        )
        assert len(payload["trend"]) == 30
        assert payload["recent_transactions"], "最近流水不应为空"
        assert payload["net_worth"]["account_count"] >= 1

    def test_change_ratio_returns_none_when_previous_is_zero(self, session: Session) -> None:
        """上期为 0 时环比无定义，不能编一个 +100% 出来。"""
        account = accounts_service.create_account(session, name="环比测试")
        today = date.today()
        transactions_service.create_transaction(
            session,
            type=TransactionType.INCOME.value,
            account_id=account.id,
            amount_minor=parse_amount("100"),
            occurred_at=datetime.combine(today, datetime.min.time()).replace(hour=12),
        )

        payload = stats_service.dashboard(session)
        assert payload["month"]["income_change"] is None

    def test_integrity_report_is_clean_for_fresh_ledger(self, session: Session) -> None:
        report = stats_service.integrity_report(session)
        assert report["ok"] is True
        assert report["issues"] == []
        assert report["stats"]["accounts"] >= 3

    def test_cash_flow_trend_covers_requested_months(self, session: Session) -> None:
        buckets = stats_service.cash_flow_trend(session, months=6)
        assert len(buckets) == 6
        assert buckets == sorted(buckets, key=lambda item: item["month"])


# -----------------------------------------------------------------------------
# 审计
# -----------------------------------------------------------------------------
class TestAuditTrail:
    def test_transaction_edits_are_recorded(self, session: Session) -> None:
        from sqlalchemy import select

        from accountbook.db.models import AuditLog

        account = _first_account(session, "现金")
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=parse_amount("10"),
            occurred_at=datetime(2026, 3, 1, 12, 0),
        )
        transactions_service.update_transaction(session, transaction.id, amount_minor=parse_amount("99"))
        transactions_service.delete_transaction(session, transaction.id)

        entries = session.scalars(
            select(AuditLog).where(AuditLog.entity == "transaction", AuditLog.entity_id == transaction.id)
        ).all()
        actions = [entry.action for entry in entries]
        assert actions == ["create", "update", "delete"]

        update_entry = next(entry for entry in entries if entry.action == "update")
        assert update_entry.changes["amount_minor"]["to"] == parse_amount("99")
