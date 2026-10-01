"""标签 / 项目 / 成员在流水上的读写用例。

为什么单独一个文件
------------------
这条链路曾经**静默丢数据**：数据库里存着标签，接口响应里却永远是空数组
（服务层返回的键叫 `tag_names`，而响应模型的字段叫 `tags`，Pydantic 于是
套用了默认值）。现象是"界面从不显示标签"，从现象反推原因几乎不可能。

因此这里把整条链路固化下来：写入 → 读回 → 筛选 → 清空，
任何一环断掉都会立刻失败。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from accountbook.core.domain import TransactionType
from accountbook.core.errors import NotFoundError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import stats as stats_service
from accountbook.services import taxonomy as taxonomy_service
from accountbook.services import transactions as transactions_service


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


def _account(session: Session) -> int:
    return accounts_service.list_accounts(session)[0].id


def _expense(session: Session, **extra: object):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_account(session),
        amount_minor=1000,
        occurred_at=datetime(2026, 3, 1, 12, 0),
        **extra,  # type: ignore[arg-type]
    )


class TestTagAssignment:
    def test_tags_are_written_and_read_back(self, session: Session) -> None:
        """写入的标签必须能读回来 —— 这是本轮修掉的那个静默丢数据问题。"""
        travel = taxonomy_service.create_tag(session, name="出差")
        reimburse = taxonomy_service.create_tag(session, name="报销")

        transaction = _expense(session, tag_ids=[travel.id, reimburse.id])

        assert sorted(tag.name for tag in transaction.tags) == ["出差", "报销"]
        # 走一遍统计层的序列化（接口响应用的就是它），确认键名对得上
        brief = stats_service.transaction_brief(transaction)
        assert sorted(brief["tags"]) == ["出差", "报销"]
        assert "tag_names" not in brief, "旧键名必须彻底消失，否则又会出现两套命名"

    def test_tag_ids_are_deduplicated(self, session: Session) -> None:
        """重复传同一个标签 id 不应该挂两次。"""
        tag = taxonomy_service.create_tag(session, name="出差")
        transaction = _expense(session, tag_ids=[tag.id, tag.id, tag.id])
        assert len(transaction.tags) == 1

    def test_unknown_tag_rejects_whole_operation(self, session: Session) -> None:
        """有一个 id 不存在就整笔失败，而不是"部分挂上、部分没挂"。"""
        good = taxonomy_service.create_tag(session, name="出差")
        with pytest.raises(NotFoundError, match="标签"):
            _expense(session, tag_ids=[good.id, 999999])

    def test_soft_deleted_tag_cannot_be_attached(self, session: Session) -> None:
        tag = taxonomy_service.create_tag(session, name="临时")
        # 未被引用时可以直接删
        taxonomy_service.delete_tag(session, tag.id)
        with pytest.raises(NotFoundError, match="标签"):
            _expense(session, tag_ids=[tag.id])

    def test_tags_can_be_replaced_and_cleared(self, session: Session) -> None:
        first = taxonomy_service.create_tag(session, name="出差")
        second = taxonomy_service.create_tag(session, name="报销")
        transaction = _expense(session, tag_ids=[first.id])
        assert [tag.name for tag in transaction.tags] == ["出差"]

        transactions_service.update_transaction(session, transaction.id, tag_ids=[second.id])
        session.refresh(transaction)
        assert [tag.name for tag in transaction.tags] == ["报销"], "替换应整体生效"

        transactions_service.update_transaction(session, transaction.id, tag_ids=[])
        session.refresh(transaction)
        assert transaction.tags == [], "传空数组应清空标签"

    def test_omitting_tags_leaves_them_untouched(self, session: Session) -> None:
        """更新其它字段时不能顺手把标签清掉。"""
        tag = taxonomy_service.create_tag(session, name="出差")
        transaction = _expense(session, tag_ids=[tag.id])

        transactions_service.update_transaction(session, transaction.id, payee="改了商户")
        session.refresh(transaction)
        assert [item.name for item in transaction.tags] == ["出差"]

    def test_project_and_member_are_assignable(self, session: Session) -> None:
        project = taxonomy_service.create_project(session, name="装修")
        member = taxonomy_service.create_member(session, name="我", is_self=True)

        transaction = _expense(session, project_id=project.id, member_id=member.id)
        brief = stats_service.transaction_brief(transaction)
        assert brief["project_name"] == "装修"
        assert brief["member_name"] == "我"

    def test_filter_by_tag(self, session: Session) -> None:
        tagged = taxonomy_service.create_tag(session, name="出差")
        _expense(session, tag_ids=[tagged.id], payee="高铁票")
        _expense(session, payee="午饭")

        query = transactions_service.TransactionQuery(tag_ids=[tagged.id])
        items, total = transactions_service.list_transactions(session, query)
        assert total == 1
        assert items[0].payee == "高铁票"

    def test_tag_delete_blocked_after_use(self, session: Session) -> None:
        """被流水引用的标签不可删除 —— 否则报表里会出现找不到标签的空洞。"""
        from accountbook.core.errors import ConflictError

        tag = taxonomy_service.create_tag(session, name="出差")
        _expense(session, tag_ids=[tag.id])
        with pytest.raises(ConflictError, match="流水"):
            taxonomy_service.delete_tag(session, tag.id)


class TestSchemaContract:
    """接口契约层面的一致性 —— 键名对不上就等于丢数据。"""

    def test_out_model_declares_every_brief_key(self) -> None:
        from accountbook.api.schemas import TransactionOut

        declared = set(TransactionOut.model_fields)
        brief_keys = {
            "id",
            "type",
            "direction",
            "occurred_at",
            "amount_minor",
            "currency",
            "payee",
            "note",
            "status",
            "account_id",
            "account_name",
            "to_account_id",
            "to_account_name",
            "category_id",
            "category_name",
            "category_icon",
            "category_color",
            "tags",
            "project_id",
            "project_name",
            "member_id",
            "member_name",
        }
        missing = brief_keys - declared
        assert not missing, f"响应模型缺少这些字段：{sorted(missing)}"

    def test_request_models_accept_tag_ids(self) -> None:
        from accountbook.api.schemas import TransactionCreate, TransactionUpdate

        assert "tag_ids" in TransactionCreate.model_fields
        assert "tag_ids" in TransactionUpdate.model_fields
