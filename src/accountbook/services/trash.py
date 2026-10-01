"""回收站（P1 尾巴 T4）。

为什么做成**泛化**的一处实现
----------------------------
软删除实体有 12 个。如果每个实体各写一套"列表 / 恢复 / 彻底删除"，
就会有 36 个几乎相同的函数，而其中的差异（谁需要顺带标脏、谁被引用时不能删）
迟早会在某一份里漏掉。

因此这里只做三件事：
1. 用一张注册表描述"有哪些实体、怎么展示"；
2. 恢复一律**委托给各领域服务自己的 restore**（它们带有副作用，
   例如恢复流水要重算日结、恢复账户要影响整条净值曲线）；
3. 彻底删除的外键依赖**从 SQLAlchemy 元数据自动推导**，而不是手工列清单 ——
   手工清单在加字段时必然漏更新，漏掉的后果是"删完留下悬空引用"。

关于"被引用就不能彻底删除"
--------------------------
引用计数**包含已软删除的行**：它们仍然持有外键。把父行物理删掉会让那些行
指向一个不存在的目标，之后恢复它们就会得到一个坏掉的对象。
因此这里宁可拒绝删除并说清是被什么挡住了，也不做级联删除 ——
级联删用户数据是不可逆的，不该由一个"清理回收站"的按钮触发。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.base import Base
from ..db.models import (
    Account,
    Budget,
    Category,
    DayEvent,
    Debt,
    Institution,
    Member,
    Project,
    RecurringRule,
    Tag,
    Transaction,
    TransactionTemplate,
)
from . import accounts as accounts_service
from . import transactions as transactions_service

_logger = logging.getLogger(__name__)

__all__ = [
    "ENTITY_KEYS",
    "TrashEntity",
    "get_entity",
    "list_trash",
    "purge",
    "restore",
    "summary",
]


@dataclass(frozen=True, slots=True)
class TrashEntity:
    """一个可回收实体的展示与行为描述。"""

    key: str
    model: type
    #: 展示用的字段（列表主标题 / 副标题）
    title_field: str
    subtitle_field: str | None = None
    #: 恢复时的委托目标。为 None 表示直接 ``obj.restore()`` 即可
    restore: Callable[[Session, int], Any] | None = None
    #: 排序字段（最近的删除排最前）
    order_field: str = "deleted_at"


ENTITIES: tuple[TrashEntity, ...] = (
    TrashEntity(
        "transactions",
        Transaction,
        "payee",
        "note",
        restore=transactions_service.restore_transaction,
    ),
    TrashEntity("accounts", Account, "name", "institution", restore=accounts_service.restore_account),
    TrashEntity("categories", Category, "name", None),
    TrashEntity("tags", Tag, "name", None),
    TrashEntity("projects", Project, "name", "note"),
    TrashEntity("members", Member, "name", "note"),
    TrashEntity("events", DayEvent, "title", "body"),
    TrashEntity("recurring", RecurringRule, "name", "note"),
    TrashEntity("budgets", Budget, "name", "note"),
    TrashEntity("debts", Debt, "name", "counterparty"),
    TrashEntity("templates", TransactionTemplate, "name", "payee"),
    TrashEntity("institutions", Institution, "name", "key"),
)

ENTITY_KEYS: tuple[str, ...] = tuple(item.key for item in ENTITIES)
_BY_KEY = {item.key: item for item in ENTITIES}


def get_entity(key: str) -> TrashEntity:
    entity = _BY_KEY.get(key)
    if entity is None:
        raise ValidationError(f"未知的回收站实体：{key}", field="entity", allowed=list(ENTITY_KEYS))
    return entity


def _serialize(entity: TrashEntity, row: Any) -> dict[str, Any]:
    title = getattr(row, entity.title_field, "") or ""
    subtitle = getattr(row, entity.subtitle_field, "") if entity.subtitle_field else ""
    deleted_at = getattr(row, "deleted_at", None)
    return {
        "entity": entity.key,
        "id": row.id,
        "title": str(title),
        "subtitle": str(subtitle or ""),
        "deleted_at": deleted_at.isoformat() if deleted_at else None,
    }


def summary(session: Session) -> list[dict[str, Any]]:
    """每个实体各有几条在回收站里。

    一次返回全部计数（12 条 COUNT），而不是让前端逐个实体去问 ——
    界面要显示"流水 3 · 账户 1"这样的概览，逐次请求会明显变慢。
    """
    result: list[dict[str, Any]] = []
    for entity in ENTITIES:
        count = session.scalar(
            select(func.count()).select_from(entity.model).where(entity.model.deleted_at.is_not(None))
        )
        result.append({"entity": entity.key, "count": int(count or 0)})
    return result


def list_trash(session: Session, entity_key: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    entity = get_entity(entity_key)
    where = entity.model.deleted_at.is_not(None)
    total = session.scalar(select(func.count()).select_from(entity.model).where(where))
    rows = session.scalars(
        select(entity.model)
        .where(where)
        .order_by(getattr(entity.model, entity.order_field).desc(), entity.model.id.desc())
        .limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    ).all()
    return {
        "entity": entity.key,
        "items": [_serialize(entity, row) for row in rows],
        "total": int(total or 0),
    }


def _load(session: Session, entity: TrashEntity, row_id: int) -> Any:
    """取出**已软删除**的那一行。未删除的不算"在回收站里"。"""
    row = session.get(entity.model, row_id)
    if row is None:
        raise NotFoundError("记录不存在", entity=entity.key, entity_id=row_id)
    if getattr(row, "deleted_at", None) is None:
        raise ConflictError("该记录不在回收站里", entity=entity.key, entity_id=row_id)
    return row


def restore(session: Session, entity_key: str, row_id: int) -> dict[str, Any]:
    """恢复一条。

    优先走领域服务自己的 restore：恢复流水要重算日结、恢复账户会改变整条
    净值曲线 —— 直接 ``obj.restore()`` 会让缓存与那些曲线停在旧值上。
    """
    entity = get_entity(entity_key)
    # **先**确认它在回收站里，再委托。领域服务的 restore 是幂等的
    # （重复调用不报错），因此不先校验的话，"恢复一个本来就没删的记录"
    # 会静默成功 —— 对界面上的过期状态来说，静默成功比报错更难排查。
    _load(session, entity, row_id)

    if entity.restore is not None:
        restored = entity.restore(session, row_id)
        return _serialize(entity, restored)

    row = _load(session, entity, row_id)
    row.restore()
    session.flush()
    return _serialize(entity, row)


def cascaded_tables(model: type) -> set[str]:
    """ORM 会**自动级联删除**的子表名。

    这些是"父行的组成部分"（例如流水的分账），删父行时应当一起消失，
    不该阻止我们清理回收站。依据是代码里的 ``cascade`` 配置本身，
    而不是手工维护的清单 —— 手工清单改 relationship 时必然漏更新。
    """
    tables: set[str] = set()
    for relation in inspect(model).relationships:
        # 多对多的**关联表**：SQLAlchemy 在删除父行时会自动清掉这些行，
        # 因此它们同样不算外部引用（`transaction_tags` 就是这种情况 ——
        # 它的 relationship 上没有 delete 级联，但关联行确实会被删）
        if relation.secondary is not None:
            tables.add(relation.secondary.name)
            continue
        cascade = relation.cascade or ""
        if ("delete" in cascade or "delete-orphan" in cascade) and relation.uselist:
            tables.add(relation.mapper.local_table.name)
    return tables


def blocking_references(session: Session, entity: TrashEntity, row_id: int) -> list[dict[str, Any]]:
    """哪些表还在通过外键引用这一行。

    从 ``Base.metadata`` 推导，而不是手工维护清单：手工清单在加字段时
    必然漏更新，而漏掉的后果是删完留下悬空引用。
    其中**级联子表被排除**：它们会随父行一起删除，不算"外部引用"。
    """
    references: list[dict[str, Any]] = []
    cascaded = cascaded_tables(entity.model)
    for table in Base.metadata.sorted_tables:
        if table.name in cascaded:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name != entity.model.__tablename__:
                continue
            if fk.column.name != "id":
                continue
            count = session.scalar(select(func.count()).select_from(table).where(fk.parent == row_id))
            if count:
                references.append({"table": table.name, "column": fk.parent.name, "count": int(count)})
    return references


def purge(session: Session, entity_key: str, row_id: int) -> None:
    """彻底删除（不可恢复）。

    有外键引用时**拒绝**并说明被什么挡住。刻意不做级联删除：
    级联删用户数据不可逆，不该由一个"清理回收站"的按钮触发。
    """
    entity = get_entity(entity_key)
    row = _load(session, entity, row_id)

    references = blocking_references(session, entity, row_id)
    if references:
        # 引用计数包含已软删除的行 —— 它们仍持有外键，父行删掉会让它们指向空。
        # 消息里**直接写出被谁引用**：只说"有引用"会让用户无处下手。
        detail = "、".join(f"{item['table']}（{item['count']} 条）" for item in references)
        raise ConflictError(
            f"仍有记录引用它：{detail}，无法彻底删除",
            entity=entity.key,
            entity_id=row_id,
            references=references,
            suggestion="先处理引用它的记录，或把它留在回收站里",
        )

    session.delete(row)
    session.flush()
    _logger.info("彻底删除 %s#%s", entity.key, row_id)
