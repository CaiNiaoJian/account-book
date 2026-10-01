"""数据层用例：迁移、约束、软删除与种子数据。

这些用例保护的是**数据完整性**——记账应用里最难挽回的错误不是崩溃，
而是"数字悄悄错了"：余额对不上、删除后重建同名失败、转账只写了一边。
因此这里逐条把约束与不变量固化成测试。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from accountbook.core.domain import AccountType, CategoryKind, TransactionType
from accountbook.db.base import Base
from accountbook.db.migrations import current_revision, run_migrations
from accountbook.db.models import Account, AppSetting, AuditLog, Category, Currency, Transaction
from accountbook.db.seed import SEED_VERSION, ensure_seed_data, seed_categories
from accountbook.db.session import Database


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """一个迁移到最新版本的临时账本库。"""
    db = Database(tmp_path / "ledger.db")
    run_migrations(db)
    yield db
    db.dispose()


# -----------------------------------------------------------------------------
# 迁移
# -----------------------------------------------------------------------------
class TestMigrations:
    def test_upgrade_creates_every_table(self, database: Database) -> None:
        """迁移后所有模型表都必须存在（漏表 = 运行时才炸）。"""
        actual = set(inspect(database.engine).get_table_names())
        expected = set(Base.metadata.tables) | {"alembic_version"}
        assert expected <= actual, f"缺少表：{expected - actual}"

    def test_revision_is_recorded(self, database: Database) -> None:
        assert current_revision(database) is not None

    def test_migration_is_idempotent(self, database: Database) -> None:
        """重复执行迁移不应有任何变化（每次启动都会调用它）。"""
        before = current_revision(database)
        after = run_migrations(database)
        assert before == after

    def test_pragmas_are_applied(self, database: Database) -> None:
        """WAL 与外键必须真的生效 —— 它们决定并发行为与引用完整性。"""
        from sqlalchemy import text

        with database.engine.connect() as connection:
            journal = connection.execute(text("PRAGMA journal_mode")).scalar()
            foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar()
        assert str(journal).lower() == "wal"
        assert int(foreign_keys) == 1


# -----------------------------------------------------------------------------
# 约束（数据库层不变量）
# -----------------------------------------------------------------------------
class TestConstraints:
    def test_account_name_is_unique_among_active(self, database: Database) -> None:
        with database.session() as session:
            session.add(Account(name="招商银行", type=AccountType.DEBIT_CARD.value))

        with pytest.raises(IntegrityError), database.session() as session:
            session.add(Account(name="招商银行", type=AccountType.DEBIT_CARD.value))

    def test_name_can_be_reused_after_soft_delete(self, database: Database) -> None:
        """软删除后应能重建同名账户 —— 这是部分唯一索引存在的意义。"""
        with database.session() as session:
            account = Account(name="测试卡", type=AccountType.DEBIT_CARD.value)
            session.add(account)
            session.flush()
            account.soft_delete()

        with database.session() as session:
            session.add(Account(name="测试卡", type=AccountType.DEBIT_CARD.value))
            session.flush()  # 不应抛异常

        with database.session() as session:
            names = session.scalars(select(Account.name)).all()
        assert names.count("测试卡") == 2

    def test_transfer_requires_target_account(self, database: Database) -> None:
        """转账缺目标账户必须被数据库拒绝（不能只靠调用方自觉）。"""
        with database.session() as session:
            source = Account(name="A", type=AccountType.CASH.value)
            session.add(source)
            session.flush()
            source_id = source.id

        with pytest.raises(IntegrityError), database.session() as session:
            session.add(
                Transaction(
                    type=TransactionType.TRANSFER.value,
                    account_id=source_id,
                    amount_minor=100,
                    occurred_at=datetime(2026, 1, 1, 12, 0),
                )
            )

    def test_non_transfer_rejects_target_account(self, database: Database) -> None:
        with database.session() as session:
            first = Account(name="A", type=AccountType.CASH.value)
            second = Account(name="B", type=AccountType.CASH.value)
            session.add_all([first, second])
            session.flush()
            ids = (first.id, second.id)

        with pytest.raises(IntegrityError), database.session() as session:
            session.add(
                Transaction(
                    type=TransactionType.EXPENSE.value,
                    account_id=ids[0],
                    to_account_id=ids[1],
                    amount_minor=100,
                    occurred_at=datetime(2026, 1, 1, 12, 0),
                )
            )

    def test_amount_must_be_positive(self, database: Database) -> None:
        with database.session() as session:
            account = Account(name="A", type=AccountType.CASH.value)
            session.add(account)
            session.flush()
            account_id = account.id

        with pytest.raises(IntegrityError), database.session() as session:
            session.add(
                Transaction(
                    type=TransactionType.EXPENSE.value,
                    account_id=account_id,
                    amount_minor=0,
                    occurred_at=datetime(2026, 1, 1, 12, 0),
                )
            )

    def test_external_id_is_unique_but_nulls_do_not_conflict(self, database: Database) -> None:
        """导入去重靠它；手写流水（NULL）之间不能互相冲突。"""
        with database.session() as session:
            account = Account(name="A", type=AccountType.CASH.value)
            session.add(account)
            session.flush()
            account_id = account.id
            session.add_all(
                [
                    Transaction(account_id=account_id, amount_minor=1, occurred_at=datetime(2026, 1, 1)),
                    Transaction(account_id=account_id, amount_minor=2, occurred_at=datetime(2026, 1, 2)),
                ]
            )

        with database.session() as session:
            account_id = session.scalars(select(Account.id)).first()
        assert account_id is not None

        with database.session() as session:
            session.add(
                Transaction(
                    account_id=account_id,
                    amount_minor=3,
                    occurred_at=datetime(2026, 1, 3),
                    external_id="alipay-20260103-001",
                )
            )

        with pytest.raises(IntegrityError), database.session() as session:
            session.add(
                Transaction(
                    account_id=account_id,
                    amount_minor=4,
                    occurred_at=datetime(2026, 1, 4),
                    external_id="alipay-20260103-001",
                )
            )

    def test_audit_log_has_no_soft_delete_column(self, database: Database) -> None:
        """审计日志本身是"删除的痕迹"，不该被软删除。"""
        columns = {column.name for column in AuditLog.__table__.columns}
        assert "deleted_at" not in columns


# -----------------------------------------------------------------------------
# 种子数据
# -----------------------------------------------------------------------------
class TestSeedData:
    def test_first_run_populates_everything(self, database: Database) -> None:
        with database.session() as session:
            counts = ensure_seed_data(session)

        assert counts["currencies"] > 0
        assert counts["accounts"] == 3
        # 分类树必须"精细且齐全"：两个方向合计远超 60 个节点
        assert counts["categories"] > 60
        # P2：机构与卡面字典（卡片墙的配色与卡面依赖它们）
        assert counts["institutions"] >= 20
        assert counts["card_artworks"] >= 6

    def test_seed_is_idempotent(self, database: Database) -> None:
        with database.session() as session:
            first = ensure_seed_data(session)
        with database.session() as session:
            second = ensure_seed_data(session)

        assert first["categories"] > 0
        assert second == {
            "currencies": 0,
            "categories": 0,
            "accounts": 0,
            "institutions": 0,
            "card_artworks": 0,
        }

    def test_seed_does_not_overwrite_user_edits(self, database: Database) -> None:
        """用户改过的内置分类，在后续补齐时必须保留。"""
        with database.session() as session:
            ensure_seed_data(session)

        with database.session() as session:
            category = session.scalars(
                select(Category).where(Category.name == "餐饮", Category.parent_id.is_(None))
            ).one()
            category.name = "吃饭（我改的）"
            category.icon = "custom"
            session.flush()

        with database.session() as session:
            ensure_seed_data(session)
        with database.session() as session:
            names = set(session.scalars(select(Category.name)).all())

        assert "吃饭（我改的）" in names
        assert "餐饮" not in names

    def test_seed_materializes_path_and_depth(self, database: Database) -> None:
        """物化路径与层级必须正确 —— 子树查询完全依赖它们。"""
        with database.session() as session:
            ensure_seed_data(session)

        with database.session() as session:
            root = session.scalars(
                select(Category).where(Category.name == "餐饮", Category.parent_id.is_(None))
            ).one()
            children = session.scalars(select(Category).where(Category.parent_id == root.id)).all()

        assert root.depth == 0
        assert root.path == f"/{root.id}"
        assert children, "餐饮下应有子分类"
        for child in children:
            assert child.depth == 1
            assert child.path.startswith(root.path + "/")
            assert child.kind == CategoryKind.EXPENSE.value

    def test_income_and_expense_kinds_are_separated(self, database: Database) -> None:
        """收入与支出分类不得混在一起：把"工资"记成支出是明显的记账错误。"""
        with database.session() as session:
            ensure_seed_data(session)

        with database.session() as session:
            kinds = set(session.scalars(select(Category.kind)).all())

        assert kinds == {CategoryKind.EXPENSE.value, CategoryKind.INCOME.value}

    def test_second_seed_round_adds_only_new_nodes(self, database: Database) -> None:
        """模拟"新版本新增了分类"：只补差额，不动已有节点。"""
        with database.session() as session:
            session.add(
                Category(
                    name="餐饮",
                    kind=CategoryKind.EXPENSE.value,
                    depth=0,
                    path="/1",
                    is_system=True,
                )
            )

        with database.session() as session:
            added = seed_categories(session)

        # 已存在的"餐饮"被跳过，其余照常补齐
        with database.session() as session:
            roots = session.scalars(
                select(Category.name).where(Category.parent_id.is_(None), Category.name == "餐饮")
            ).all()
        assert len(roots) == 1, "同名根分类不应重复写入"
        assert added > 0

    def test_currencies_carry_minor_units(self, database: Database) -> None:
        """日元没有小数位；若按两位处理，所有金额都会差 100 倍。"""
        with database.session() as session:
            ensure_seed_data(session)
            jpy = session.get(Currency, "JPY")
            cny = session.get(Currency, "CNY")

        assert jpy is not None and jpy.minor_units == 0
        assert cny is not None and cny.minor_units == 2

    def test_seed_version_recorded(self, database: Database) -> None:
        with database.session() as session:
            ensure_seed_data(session)
        with database.session() as session:
            setting = session.get(AppSetting, "seed_version")

        assert setting is not None
        assert setting.value["version"] == SEED_VERSION


# -----------------------------------------------------------------------------
# 金额在数据库层保持整数
# -----------------------------------------------------------------------------
class TestMoneyStorage:
    def test_amounts_round_trip_as_integers(self, database: Database) -> None:
        """数据库里必须是整数 —— 一旦变成浮点，精度就已经丢了。"""
        from accountbook.core.money import from_minor, parse_amount

        minor = parse_amount("¥1,234.56")
        with database.session() as session:
            account = Account(name="A", type=AccountType.CASH.value)
            session.add(account)
            session.flush()
            session.add(
                Transaction(
                    account_id=account.id,
                    amount_minor=minor,
                    occurred_at=datetime(2026, 1, 1, 12, 0),
                )
            )

        with database.session() as session:
            stored = session.scalars(select(Transaction.amount_minor)).one()

        assert isinstance(stored, int)
        assert stored == 123456
        assert from_minor(stored) == Decimal("1234.56")
