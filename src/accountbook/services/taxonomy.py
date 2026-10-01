"""标签 / 项目 / 成员服务。

这三者是同一类东西：**没有层级、字段极少、被流水引用**的横向维度。
因此共用一个泛型实现，而不是把几乎相同的代码抄三遍 —— 抄三遍意味着
将来加一个字段要改三处，且必然漏掉其中一处。

差异化部分（是否有唯一名约束、删除时的引用检查）通过参数声明。
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import Member, Project, Tag, Transaction
from . import audit

__all__ = [
    "MEMBER_FIELDS",
    "PROJECT_FIELDS",
    "TAG_FIELDS",
    "create_member",
    "create_project",
    "create_tag",
    "delete_member",
    "delete_project",
    "delete_tag",
    "list_members",
    "list_projects",
    "list_tags",
    "update_member",
    "update_project",
    "update_tag",
]

_logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", Tag, Project, Member)

TAG_FIELDS: frozenset[str] = frozenset({"name", "color", "note"})
PROJECT_FIELDS: frozenset[str] = frozenset({"name", "color", "status", "budget_minor", "note"})
MEMBER_FIELDS: frozenset[str] = frozenset({"name", "color", "is_self", "note"})

#: 实体名 → (模型, 可写字段, 唯一名, 流水引用列)
_SPECS: dict[str, tuple[type[Any], frozenset[str], bool, str]] = {
    "tag": (Tag, TAG_FIELDS, True, "tag"),
    "project": (Project, PROJECT_FIELDS, False, "project_id"),
    "member": (Member, MEMBER_FIELDS, False, "member_id"),
}


# -----------------------------------------------------------------------------
# 通用实现
# -----------------------------------------------------------------------------
def _model(entity: str) -> type[Any]:
    spec = _SPECS.get(entity)
    if spec is None:  # pragma: no cover - 内部编程错误
        raise ValueError(f"未知实体：{entity}")
    return spec[0]


def _fields(entity: str) -> frozenset[str]:
    return _SPECS[entity][1]


def _list(session: Session, entity: str, *, include_deleted: bool = False) -> list[Any]:
    model = _model(entity)
    statement = select(model)
    if not include_deleted:
        statement = statement.where(model.deleted_at.is_(None))
    statement = statement.order_by(model.id)
    return list(session.scalars(statement).all())


def _get(session: Session, entity: str, item_id: int, *, include_deleted: bool = False) -> Any:
    model = _model(entity)
    item = session.get(model, item_id)
    if item is None or (item.deleted_at is not None and not include_deleted):
        raise NotFoundError(f"{entity} 不存在或已被删除", entity=entity, entity_id=item_id)
    return item


def _assert_name_available(
    session: Session, entity: str, name: str, *, exclude_id: int | None = None
) -> None:
    """校验重名（仅对声明了"唯一名"的实体生效）。"""
    if not _SPECS[entity][2]:
        return
    model = _model(entity)
    statement = select(model.id).where(model.name == name, model.deleted_at.is_(None))
    if exclude_id is not None:
        statement = statement.where(model.id != exclude_id)
    if session.scalar(statement.limit(1)) is not None:
        raise ConflictError(f"已存在同名{entity}：{name}", field="name", value=name)


def _create(session: Session, entity: str, fields: dict[str, Any]) -> Any:
    allowed = _fields(entity)
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(f"不支持的字段：{sorted(unknown)}", fields=sorted(unknown))

    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValidationError("名称不能为空", field="name")
    fields["name"] = name
    _assert_name_available(session, entity, name)

    item = _model(entity)(**fields)
    session.add(item)
    session.flush()
    audit.record(session, entity=entity, entity_id=item.id, action="create", changes={"name": {"to": name}})
    return item


def _update(session: Session, entity: str, item_id: int, changes: dict[str, Any]) -> Any:
    allowed = _fields(entity)
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"不支持的字段：{sorted(unknown)}", fields=sorted(unknown))

    item = _get(session, entity, item_id)
    if "name" in changes:
        new_name = str(changes["name"] or "").strip()
        if not new_name:
            raise ValidationError("名称不能为空", field="name")
        changes["name"] = new_name
        _assert_name_available(session, entity, new_name, exclude_id=item_id)

    before = audit.snapshot(item, changes.keys())
    for key, value in changes.items():
        setattr(item, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity=entity,
        entity_id=item_id,
        action="update",
        before=before,
        after={key: getattr(item, key) for key in changes},
    )
    return item


def _delete(session: Session, entity: str, item_id: int) -> None:
    """软删除。被流水引用时拒绝 —— 与账户/分类一致的处理方式。"""
    item = _get(session, entity, item_id)
    reference_column = _SPECS[entity][3]

    if reference_column == "tag":
        used = session.scalar(
            select(func.count(Transaction.id))
            .select_from(Transaction)
            .join(Transaction.tags)
            .where(Tag.id == item_id, Transaction.deleted_at.is_(None))
        )
    else:
        column = getattr(Transaction, reference_column)
        used = session.scalar(
            select(func.count(Transaction.id)).where(column == item_id, Transaction.deleted_at.is_(None))
        )

    if used:
        raise ConflictError(
            f"该{entity}已被 {int(used or 0)} 笔流水使用，请先解除引用",
            entity=entity,
            entity_id=item_id,
            transaction_count=int(used or 0),
        )

    item.soft_delete()
    session.flush()
    audit.record(session, entity=entity, entity_id=item_id, action="delete")


# -----------------------------------------------------------------------------
# 标签
# -----------------------------------------------------------------------------
def list_tags(session: Session, *, include_deleted: bool = False) -> list[Tag]:
    return _list(session, "tag", include_deleted=include_deleted)


def create_tag(session: Session, **fields: Any) -> Tag:
    return _create(session, "tag", fields)


def update_tag(session: Session, tag_id: int, **changes: Any) -> Tag:
    return _update(session, "tag", tag_id, changes)


def delete_tag(session: Session, tag_id: int) -> None:
    _delete(session, "tag", tag_id)


# -----------------------------------------------------------------------------
# 项目
# -----------------------------------------------------------------------------
def list_projects(session: Session, *, include_deleted: bool = False) -> list[Project]:
    return _list(session, "project", include_deleted=include_deleted)


def create_project(session: Session, **fields: Any) -> Project:
    return _create(session, "project", fields)


def update_project(session: Session, project_id: int, **changes: Any) -> Project:
    return _update(session, "project", project_id, changes)


def delete_project(session: Session, project_id: int) -> None:
    _delete(session, "project", project_id)


# -----------------------------------------------------------------------------
# 成员
# -----------------------------------------------------------------------------
def list_members(session: Session, *, include_deleted: bool = False) -> list[Member]:
    return _list(session, "member", include_deleted=include_deleted)


def _clear_other_self_flags(session: Session, member: Member) -> None:
    """确保"账本主人"只有一个。

    逐个用 ORM 对象改，而不是发一条批量 UPDATE：
    批量 UPDATE 绕过会话，内存里其它成员对象的 ``is_self`` 会停留在旧值，
    调用方随后读到的是过期数据（"设了两个主人"的假象）。
    成员数量是个位数，逐个改的成本可以忽略。
    """
    others = session.scalars(select(Member).where(Member.id != member.id, Member.is_self.is_(True))).all()
    for other in others:
        other.is_self = False
    session.flush()


def create_member(session: Session, **fields: Any) -> Member:
    member = _create(session, "member", fields)
    if member.is_self:
        _clear_other_self_flags(session, member)
    return member


def update_member(session: Session, member_id: int, **changes: Any) -> Member:
    member = _update(session, "member", member_id, changes)
    if changes.get("is_self"):
        _clear_other_self_flags(session, member)
    return member


def delete_member(session: Session, member_id: int) -> None:
    _delete(session, "member", member_id)
