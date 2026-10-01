"""分类服务 —— 树形结构维护、迁移与合并。

树结构的两条实现要点
--------------------
1. **物化路径**（``path`` = ``/1/7/23``）：取"某分类及其所有子分类"是最高频的
   查询（报表按大类汇总）。有了路径就退化成 ``path LIKE '/1/7/%'``，
   比递归 CTE 在 SQLite 上更快，且能被索引命中。
2. **移动要级联**：把一个分类挂到别处时，它的整棵子树的 ``path`` 与 ``depth``
   都必须重算 —— 只改自身是这类实现最常见的 bug（子树的路径会指向错误祖先）。

为什么内置分类不可删除
----------------------
它们承担"开箱可用"的职责。允许删除会导致新用户误删后无分类可用，
而恢复需要手工重建整棵树。折中：**可改名、可改图标、可隐藏、可合并，但不可删**。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..core.domain import CategoryKind
from ..core.errors import ConflictError, NotFoundError, ProtectedEntityError, ValidationError
from ..db.models import Category, Transaction, TransactionSplit
from . import audit

__all__ = [
    "CATEGORY_MUTABLE_FIELDS",
    "MAX_DEPTH",
    "create_category",
    "delete_category",
    "descendant_ids",
    "get_category",
    "list_categories",
    "merge_category",
    "move_category",
    "set_hidden",
    "tree",
    "update_category",
]

_logger = logging.getLogger(__name__)

#: 树的最大深度（与数据库 CHECK 约束保持一致）。
#: 限制深度不是技术限制，而是**可用性**考虑：超过 3 层的分类在记账时
#: 需要点 4 次才能选中，用户坚持不下来。
MAX_DEPTH = 5

CATEGORY_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {"name", "kind", "parent_id", "icon", "color", "is_hidden", "sort_order", "note", "meta"}
)


# -----------------------------------------------------------------------------
# 查询
# -----------------------------------------------------------------------------
def list_categories(
    session: Session,
    *,
    kind: str | None = None,
    include_hidden: bool = True,
    include_deleted: bool = False,
) -> list[Category]:
    statement = select(Category)
    if not include_deleted:
        statement = statement.where(Category.deleted_at.is_(None))
    if kind is not None:
        statement = statement.where(Category.kind == kind)
    if not include_hidden:
        statement = statement.where(Category.is_hidden.is_(False))
    statement = statement.order_by(Category.depth, Category.sort_order, Category.id)
    return list(session.scalars(statement).all())


def get_category(session: Session, category_id: int, *, include_deleted: bool = False) -> Category:
    category = session.get(Category, category_id)
    if category is None or (category.is_deleted and not include_deleted):
        raise NotFoundError("分类不存在或已被删除", entity="category", entity_id=category_id)
    return category


def tree(session: Session, *, kind: str | None = None, include_hidden: bool = True) -> list[dict[str, Any]]:
    """返回嵌套分类树。

    一次取全表后在内存里组装，而不是逐级查询：
    分类总数在数百量级，一次全取只有一个往返；逐级查询会产生几十次往返
    （每层一次），在树较深时明显更慢。
    """
    rows = list_categories(session, kind=kind, include_hidden=include_hidden)
    nodes: dict[int, dict[str, Any]] = {
        row.id: {
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "parent_id": row.parent_id,
            "icon": row.icon,
            "color": row.color,
            "is_system": row.is_system,
            "is_hidden": row.is_hidden,
            "sort_order": row.sort_order,
            "depth": row.depth,
            "path": row.path,
            "note": row.note,
            "children": [],
        }
        for row in rows
    }

    roots: list[dict[str, Any]] = []
    for row in rows:
        node = nodes[row.id]
        parent = nodes.get(row.parent_id) if row.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


def descendant_ids(session: Session, category_id: int, *, include_self: bool = True) -> list[int]:
    """返回某分类的全部后代 id（含自身，按需）。

    用物化路径一次查完，避免递归。
    """
    category = get_category(session, category_id, include_deleted=True)
    prefix = f"{category.path}/"
    rows = session.scalars(
        select(Category.id).where(
            Category.deleted_at.is_(None),
            Category.path.like(f"{prefix}%"),
        )
    ).all()
    ids = [int(item) for item in rows]
    return [category.id, *ids] if include_self else ids


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def _validate_kind(kind: str) -> str:
    if kind not in {member.value for member in CategoryKind}:
        raise ValidationError(f"未知分类方向：{kind}", field="kind")
    return kind


def _assert_sibling_name_available(
    session: Session, name: str, parent_id: int | None, *, exclude_id: int | None = None
) -> None:
    """同一父节点下不允许重名（软删除的不算）。

    数据库层没有为它建唯一索引：SQLite 的唯一索引把 NULL 视为互不相同，
    "根级分类"（``parent_id IS NULL``）之间的重名不会被拦住，
    因此必须在这里校验，否则根级会出现两个"餐饮"。
    """
    statement = select(Category.id).where(
        Category.name == name,
        Category.deleted_at.is_(None),
    )
    statement = (
        statement.where(Category.parent_id.is_(None))
        if parent_id is None
        else statement.where(Category.parent_id == parent_id)
    )
    if exclude_id is not None:
        statement = statement.where(Category.id != exclude_id)
    if session.scalar(statement.limit(1)) is not None:
        raise ConflictError(f"同级下已存在同名分类：{name}", field="name", value=name)


def create_category(session: Session, **fields: Any) -> Category:
    unknown = set(fields) - CATEGORY_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的分类字段：{sorted(unknown)}", fields=sorted(unknown))

    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValidationError("分类名不能为空", field="name")
    fields["name"] = name
    fields["kind"] = _validate_kind(str(fields.get("kind") or CategoryKind.EXPENSE.value))

    parent: Category | None = None
    parent_id = fields.get("parent_id")
    if parent_id is not None:
        parent = get_category(session, int(parent_id))
        if parent.kind != fields["kind"]:
            raise ValidationError(
                "子分类的方向必须与父分类一致", parent_kind=parent.kind, child_kind=fields["kind"]
            )
        if parent.depth + 1 > MAX_DEPTH:
            raise ValidationError(f"分类层级不能超过 {MAX_DEPTH} 层", max_depth=MAX_DEPTH)

    _assert_sibling_name_available(session, name, parent.id if parent else None)

    # 先取出会与显式参数冲突的字段。
    # 教训：``Category(**fields, sort_order=…)`` 在 payload 已含 sort_order 时
    # 会抛 "got multiple values for keyword argument" —— 这是 500 而不是 4xx，
    # 因为它是编程错误；写成 pop 之后两种来源都能正确工作。
    sort_order = int(fields.pop("sort_order", None) or 100)
    category = Category(
        **fields,
        depth=(parent.depth + 1) if parent else 0,
        sort_order=sort_order,
    )
    session.add(category)
    session.flush()
    # 路径依赖自增 id，必须在 flush 之后补写
    category.path = f"{parent.path}/{category.id}" if parent else f"/{category.id}"
    session.flush()

    audit.record(
        session, entity="category", entity_id=category.id, action="create", changes={"name": {"to": name}}
    )
    return category


def update_category(session: Session, category_id: int, **changes: Any) -> Category:
    unknown = set(changes) - (CATEGORY_MUTABLE_FIELDS - {"parent_id"})
    if unknown:
        raise ValidationError(f"不支持的分类字段：{sorted(unknown)}", fields=sorted(unknown))

    category = get_category(session, category_id)
    if "name" in changes:
        new_name = str(changes["name"] or "").strip()
        if not new_name:
            raise ValidationError("分类名不能为空", field="name")
        changes["name"] = new_name
        _assert_sibling_name_available(session, new_name, category.parent_id, exclude_id=category_id)

    before = audit.snapshot(category, changes.keys())
    for key, value in changes.items():
        setattr(category, key, value)
    session.flush()

    audit.record_diff(
        session,
        entity="category",
        entity_id=category_id,
        action="update",
        before=before,
        after={key: getattr(category, key) for key in changes},
    )
    return category


def set_hidden(session: Session, category_id: int, hidden: bool) -> Category:
    """隐藏 / 显示分类（内置分类"不可删除"的替代方案）。"""
    category = get_category(session, category_id)
    if category.is_hidden == hidden:
        return category
    category.is_hidden = hidden
    session.flush()
    audit.record(
        session,
        entity="category",
        entity_id=category_id,
        action="update",
        changes={"is_hidden": {"to": hidden}},
    )
    return category


def move_category(session: Session, category_id: int, new_parent_id: int | None) -> Category:
    """把分类（连同整棵子树）移动到新的父节点下。

    必须级联更新子树的 ``path`` 与 ``depth`` —— 只改自身会让后代的路径
    指向错误的祖先，而这类错误只在"按大类汇总"时才会暴露，极难排查。
    """
    category = get_category(session, category_id)
    if new_parent_id == category_id:
        raise ValidationError("不能把分类移动到它自己下面", category_id=category_id)

    new_parent: Category | None = None
    if new_parent_id is not None:
        new_parent = get_category(session, int(new_parent_id))
        if new_parent.kind != category.kind:
            raise ValidationError("不能移动到方向不同的分类下", kind=category.kind)
        if new_parent.id in set(descendant_ids(session, category_id)):
            # 把父节点移到自己的后代下会形成环，整棵树随之不可达
            raise ValidationError("不能把分类移动到它自己的子分类下", category_id=category_id)

    old_prefix = f"{category.path}/"
    old_path = category.path
    old_depth = category.depth
    new_depth = (new_parent.depth + 1) if new_parent else 0
    subtree_height = _subtree_height(session, category)
    if new_depth + subtree_height > MAX_DEPTH:
        raise ValidationError(f"移动后层级将超过 {MAX_DEPTH} 层", max_depth=MAX_DEPTH)

    _assert_sibling_name_available(
        session, category.name, new_parent.id if new_parent else None, exclude_id=category_id
    )

    category.parent_id = new_parent.id if new_parent else None
    category.depth = new_depth
    session.flush()
    new_path = f"{new_parent.path}/{category.id}" if new_parent else f"/{category.id}"
    category.path = new_path

    depth_delta = new_depth - old_depth
    # 先改后代，再改自身；顺序不重要，但必须覆盖整棵子树
    session.execute(
        update(Category)
        .where(Category.path.like(f"{old_prefix}%"))
        .values(
            path=func.replace(Category.path, old_path, new_path),
            depth=Category.depth + depth_delta,
        )
    )
    session.flush()

    audit.record(
        session,
        entity="category",
        entity_id=category_id,
        action="move",
        changes={"parent_id": {"from": old_path, "to": new_path}},
    )
    return category


def _subtree_height(session: Session, category: Category) -> int:
    """子树高度（自身为 0）。用于移动前的深度校验。"""
    max_depth = session.scalar(
        select(func.max(Category.depth)).where(
            Category.deleted_at.is_(None),
            Category.path.like(f"{category.path}/%"),
        )
    )
    if max_depth is None:
        return 0
    return int(max_depth) - category.depth


def merge_category(session: Session, source_id: int, target_id: int) -> dict[str, int]:
    """把 ``source`` 合并进 ``target``：迁移流水与分账，然后软删除源分类。

    典型场景：用户自建了"吃喝"，后来发现内置的"餐饮"更合适。
    合并必须迁移**分账**（``transaction_splits``）—— 只迁移流水本体
    会留下指向已删除分类的分账，报表口径立刻出错。
    """
    if source_id == target_id:
        raise ValidationError("不能把分类合并到它自己", category_id=source_id)

    source = get_category(session, source_id)
    target = get_category(session, target_id)
    if source.kind != target.kind:
        raise ValidationError("只能合并方向相同的分类", source_kind=source.kind, target_kind=target.kind)

    moved_transactions = session.execute(
        update(Transaction).where(Transaction.category_id == source_id).values(category_id=target_id)
    ).rowcount
    moved_splits = session.execute(
        update(TransactionSplit)
        .where(TransactionSplit.category_id == source_id)
        .values(category_id=target_id)
    ).rowcount

    # 子分类一并挂到目标下，用户不必逐个手动移动
    child_count = session.execute(
        update(Category)
        .where(Category.parent_id == source_id, Category.deleted_at.is_(None))
        .values(parent_id=target_id)
    ).rowcount
    session.flush()
    for child_id in session.scalars(
        select(Category.id).where(Category.parent_id == target_id, Category.deleted_at.is_(None))
    ).all():
        _recompute_path(session, int(child_id))

    source.soft_delete()
    session.flush()
    audit.record(
        session,
        entity="category",
        entity_id=source_id,
        action="merge",
        changes={
            "into": {"to": target_id},
            "transactions": {"to": int(moved_transactions or 0)},
            "splits": {"to": int(moved_splits or 0)},
            "children": {"to": int(child_count or 0)},
        },
    )
    return {
        "transactions": int(moved_transactions or 0),
        "splits": int(moved_splits or 0),
        "children": int(child_count or 0),
    }


def _recompute_path(session: Session, category_id: int) -> None:
    """重算单个分类的 path/depth（其父节点已经确定）。"""
    category = session.get(Category, category_id)
    if category is None:
        return
    parent = session.get(Category, category.parent_id) if category.parent_id else None
    category.depth = (parent.depth + 1) if parent else 0
    category.path = f"{parent.path}/{category.id}" if parent else f"/{category.id}"


def delete_category(session: Session, category_id: int) -> None:
    """删除分类（软删除）。

    四道拒绝理由，每一条都对应一种会破坏数据一致性的情况：
        1. 内置分类 —— 见模块说明；
        2. 有子分类 —— 会让子分类失去父节点；
        3. 被流水使用 —— 会让统计口径出现空洞；
        4. 被分账使用 —— 同上，且分账更难发现。
    """
    category = get_category(session, category_id)
    if category.is_system:
        raise ProtectedEntityError(
            "内置分类不可删除，可以改名、改图标、隐藏，或合并到其它分类",
            category_id=category_id,
            suggestion="hide_or_merge",
        )

    child_count = session.scalar(
        select(func.count(Category.id)).where(
            Category.parent_id == category_id, Category.deleted_at.is_(None)
        )
    )
    if child_count:
        raise ConflictError(f"该分类下还有 {child_count} 个子分类", child_count=int(child_count or 0))

    used = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.category_id == category_id, Transaction.deleted_at.is_(None)
        )
    )
    split_used = session.scalar(
        select(func.count(TransactionSplit.id)).where(TransactionSplit.category_id == category_id)
    )
    if used or split_used:
        raise ConflictError(
            f"该分类已被 {int(used or 0)} 笔流水与 {int(split_used or 0)} 条分账使用，"
            "请先迁移到其它分类或使用合并功能",
            transaction_count=int(used or 0),
            split_count=int(split_used or 0),
            suggestion="merge",
        )

    category.soft_delete()
    session.flush()
    audit.record(session, entity="category", entity_id=category_id, action="delete")
