"""流水服务 —— 记账核心的读写与统计口径。

本模块承担全应用**最关键的职责**：保证"记下的每一笔都自洽"。
校验清单（每一条都对应一种真实的错误记账方式）：

============================  ====================================================
规则                          不做会怎样
============================  ====================================================
金额必须为正整数最小单位      负数与浮点会让收支方向出现两种等价写法
方向与类型自洽                支出被标成"增加余额"，余额越记越多
转账必须有目标且不等于源账户  钱凭空消失或自己转给自己
分类方向必须匹配流水类型      把"工资"记成支出，报表彻底失真
分账金额之和 = 流水金额       报表按分类汇总时总额对不上
============================  ====================================================

统计口径
--------
``TRANSFER_TYPES``（转账、校准）**不计入收支**。这是记账软件最常见的口径错误：
把"从工资卡转到支付宝"算成支出，用户会看到自己月支出凭空翻倍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..core.domain import TRANSFER_TYPES, CategoryKind, TransactionStatus, TransactionType
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.money import DEFAULT_CURRENCY
from ..db.base import local_now
from ..db.models import Account, Category, Tag, Transaction, TransactionSplit
from . import audit, daily

__all__ = [
    "TRANSACTION_MUTABLE_FIELDS",
    "DailyTotal",
    "SummaryResult",
    "TransactionQuery",
    "create_transaction",
    "daily_totals",
    "delete_transaction",
    "get_transaction",
    "list_transactions",
    "restore_transaction",
    "summary",
    "update_transaction",
]

_logger = logging.getLogger(__name__)

#: 可通过 API 写入的字段白名单（``splits`` 单独处理）
TRANSACTION_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "direction",
        "occurred_at",
        "tz_offset_minutes",
        "account_id",
        "to_account_id",
        "category_id",
        "amount_minor",
        "currency",
        "payee",
        "note",
        "status",
        "source",
        "external_id",
        "project_id",
        "member_id",
        "meta",
    }
)

#: 默认方向由类型决定；只有 adjust 允许自定义
_DEFAULT_DIRECTION: dict[str, str] = {
    TransactionType.EXPENSE.value: "out",
    TransactionType.INCOME.value: "in",
    TransactionType.TRANSFER.value: "out",
    TransactionType.ADJUST.value: "out",
}

_CATEGORY_KIND_FOR_TYPE: dict[str, str] = {
    TransactionType.EXPENSE.value: CategoryKind.EXPENSE.value,
    TransactionType.INCOME.value: CategoryKind.INCOME.value,
}


# -----------------------------------------------------------------------------
# 查询参数
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class TransactionQuery:
    """流水查询条件。

    集中成一个对象而不是十几个函数参数：调用方（API 层）只需构造一次，
    服务层内部也可以在 :meth:`to_statement` 里统一处理"哪些条件该拼进 SQL"。
    """

    start: datetime | None = None
    end: datetime | None = None
    account_ids: list[int] = field(default_factory=list)
    category_ids: list[int] = field(default_factory=list)
    tag_ids: list[int] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    project_id: int | None = None
    member_id: int | None = None
    min_amount_minor: int | None = None
    max_amount_minor: int | None = None
    keyword: str = ""
    include_deleted: bool = False
    include_transfers: bool = True
    order: str = "desc"
    limit: int = 50
    offset: int = 0

    def to_conditions(self) -> list[Any]:
        """把查询条件翻译成 SQLAlchemy 条件列表。"""
        conditions: list[Any] = []
        if not self.include_deleted:
            conditions.append(Transaction.deleted_at.is_(None))
        if self.start is not None:
            conditions.append(Transaction.occurred_at >= self.start)
        if self.end is not None:
            conditions.append(Transaction.occurred_at <= self.end)
        if self.account_ids:
            # 账户筛选要包含"转入"，否则从该账户收款不会出现在它的账单里
            conditions.append(
                or_(
                    Transaction.account_id.in_(self.account_ids),
                    Transaction.to_account_id.in_(self.account_ids),
                )
            )
        if self.category_ids:
            # 分账也应按分类命中：一笔拆分到"餐饮"的购物流水属于餐饮口径
            conditions.append(
                or_(
                    Transaction.category_id.in_(self.category_ids),
                    Transaction.id.in_(
                        select(TransactionSplit.transaction_id).where(
                            TransactionSplit.category_id.in_(self.category_ids)
                        )
                    ),
                )
            )
        if self.tag_ids:
            conditions.append(
                Transaction.id.in_(
                    select(Transaction.id).join(Transaction.tags).where(Tag.id.in_(self.tag_ids))
                )
            )
        if self.types:
            conditions.append(Transaction.type.in_(self.types))
        elif not self.include_transfers:
            conditions.append(Transaction.type.not_in([item.value for item in TRANSFER_TYPES]))
        if self.statuses:
            conditions.append(Transaction.status.in_(self.statuses))
        if self.project_id is not None:
            conditions.append(Transaction.project_id == self.project_id)
        if self.member_id is not None:
            conditions.append(Transaction.member_id == self.member_id)
        if self.min_amount_minor is not None:
            conditions.append(Transaction.amount_minor >= self.min_amount_minor)
        if self.max_amount_minor is not None:
            conditions.append(Transaction.amount_minor <= self.max_amount_minor)
        if self.keyword.strip():
            like = f"%{self.keyword.strip()}%"
            conditions.append(or_(Transaction.payee.like(like), Transaction.note.like(like)))
        return conditions

    def to_statement(self) -> Select[tuple[Transaction]]:
        statement = select(Transaction).options(
            selectinload(Transaction.account),
            selectinload(Transaction.to_account),
            selectinload(Transaction.category),
            selectinload(Transaction.tags),
            selectinload(Transaction.splits),
        )
        conditions = self.to_conditions()
        if conditions:
            statement = statement.where(and_(*conditions))
        order_column = (
            Transaction.occurred_at.asc() if self.order == "asc" else Transaction.occurred_at.desc()
        )
        # 同一时刻的多笔用 id 兜底，否则分页时可能出现重复/遗漏
        return statement.order_by(order_column, Transaction.id.desc())


@dataclass(slots=True)
class SummaryResult:
    """收支汇总（转账与校准已被排除）。"""

    income_minor: int = 0
    expense_minor: int = 0
    transaction_count: int = 0
    by_category: list[dict[str, Any]] = field(default_factory=list)

    @property
    def net_minor(self) -> int:
        return self.income_minor - self.expense_minor


@dataclass(slots=True)
class DailyTotal:
    """某一天的收支（日历与时间轴视图的数据源）。"""

    day: date
    income_minor: int
    expense_minor: int
    transaction_count: int


# -----------------------------------------------------------------------------
# 读取
# -----------------------------------------------------------------------------
def get_transaction(session: Session, transaction_id: int, *, include_deleted: bool = False) -> Transaction:
    transaction = session.get(Transaction, transaction_id)
    if transaction is None or (transaction.is_deleted and not include_deleted):
        raise NotFoundError("流水不存在或已被删除", entity="transaction", entity_id=transaction_id)
    return transaction


def list_transactions(session: Session, query: TransactionQuery) -> tuple[list[Transaction], int]:
    """返回 ``(当页流水, 满足条件的总数)``。

    总数单独查一次：前端要显示"共 N 笔"并据此做分页，
    而用 ``len(items)`` 冒充总数在分页场景下必然出错。
    """
    statement = query.to_statement()
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = list(session.scalars(statement.limit(query.limit).offset(query.offset)).all())
    return items, int(total)


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def create_transaction(
    session: Session,
    *,
    splits: list[dict[str, Any]] | None = None,
    tag_ids: list[int] | None = None,
    **fields: Any,
) -> Transaction:
    """创建流水（可选带分账与标签）。"""
    unknown = set(fields) - TRANSACTION_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的流水字段：{sorted(unknown)}", fields=sorted(unknown))

    normalized = _normalize_fields(session, fields, current=None)
    transaction = Transaction(**normalized)
    session.add(transaction)
    session.flush()

    if splits:
        _replace_splits(session, transaction, splits)
        session.flush()

    if tag_ids:
        _replace_tags(session, transaction, tag_ids)
        session.flush()

    # 让日结缓存失效。**放在这里而不是 API 层**：只要经过服务层写入就一定会标脏，
    # 不会因为将来多了一条调用路径（导入、周期记账、插件）而漏掉。
    daily.mark_dirty_from(session, transaction.occurred_at)

    audit.record(
        session,
        entity="transaction",
        entity_id=transaction.id,
        action="create",
        changes={
            "type": {"to": transaction.type},
            "amount_minor": {"to": transaction.amount_minor},
            "account_id": {"to": transaction.account_id},
        },
    )
    return transaction


def update_transaction(
    session: Session,
    transaction_id: int,
    *,
    splits: list[dict[str, Any]] | None = None,
    tag_ids: list[int] | None = None,
    add_tag_ids: list[int] | None = None,
    **changes: Any,
) -> Transaction:
    """更新流水。

    ``splits`` / ``tag_ids`` 的语义一致：传 ``[]`` 表示**清空**，传 ``None`` 表示**不动**。
    这个区分是必要的 —— 否则"把标签全删掉"这个操作没法表达，
    用户只能删掉整笔流水重记。
    """
    unknown = set(changes) - TRANSACTION_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的流水字段：{sorted(unknown)}", fields=sorted(unknown))

    transaction = get_transaction(session, transaction_id)
    # 记下改动前的业务日期：把一笔流水从 3 号改到 1 号，
    # 1 号到 3 号之间的日结**全部**受影响，因此要取两者的较早值标脏
    previous_day = transaction.occurred_at
    normalized = _normalize_fields(session, changes, current=transaction)

    before = audit.snapshot(transaction, normalized.keys())
    for key, value in normalized.items():
        setattr(transaction, key, value)
    session.flush()

    if splits is not None:
        _replace_splits(session, transaction, splits)
        session.flush()

    if tag_ids is not None:
        _replace_tags(session, transaction, tag_ids)
        session.flush()
    elif add_tag_ids:
        # 追加语义（批量打标签用）。与 tag_ids 互斥：同时给两者
        # 会让"到底以哪个为准"变成一个没有正确答案的问题。
        # 已挂上的标签不重复添加 —— 批量操作重复执行时不该产生重复关联
        existing = {tag.id for tag in transaction.tags}
        merged = existing | {int(item) for item in add_tag_ids}
        if merged != existing:
            _replace_tags(session, transaction, sorted(merged))
            session.flush()

    daily.mark_dirty_from(session, min(previous_day, transaction.occurred_at))

    audit.record_diff(
        session,
        entity="transaction",
        entity_id=transaction.id,
        action="update",
        before=before,
        after={key: getattr(transaction, key) for key in normalized},
    )
    return transaction


def delete_transaction(session: Session, transaction_id: int) -> None:
    """软删除流水（可在回收站恢复）。"""
    transaction = get_transaction(session, transaction_id)
    transaction.soft_delete()
    session.flush()
    daily.mark_dirty_from(session, transaction.occurred_at)
    audit.record(session, entity="transaction", entity_id=transaction_id, action="delete")


def restore_transaction(session: Session, transaction_id: int) -> Transaction:
    transaction = get_transaction(session, transaction_id, include_deleted=True)
    if transaction.is_deleted:
        transaction.restore()
        session.flush()
        daily.mark_dirty_from(session, transaction.occurred_at)
        audit.record(session, entity="transaction", entity_id=transaction_id, action="restore")
    return transaction


# -----------------------------------------------------------------------------
# 校验与归一化
# -----------------------------------------------------------------------------
def _normalize_fields(
    session: Session, fields: dict[str, Any], *, current: Transaction | None
) -> dict[str, Any]:
    """校验并补全流水字段，返回可直接赋给 ORM 的字典。

    以"合并后的最终状态"为校验对象（而不是只看本次改动的字段）：
    例如只把 ``type`` 从支出改成转账时，仍然要检查目标账户是否存在 ——
    只校验增量是这类 bug 的经典来源。
    """

    def resolved(name: str, default: Any = None) -> Any:
        if name in fields:
            return fields[name]
        return getattr(current, name, default) if current is not None else default

    transaction_type = str(resolved("type") or TransactionType.EXPENSE.value)
    if transaction_type not in {member.value for member in TransactionType}:
        raise ValidationError(f"未知流水类型：{transaction_type}", field="type")

    amount = resolved("amount_minor", 0)
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValidationError("金额必须是整数最小单位", field="amount_minor")
    if amount <= 0:
        raise ValidationError("金额必须大于 0（方向由类型与方向字段表达）", field="amount_minor")

    direction = str(resolved("direction") or _DEFAULT_DIRECTION[transaction_type])
    if direction not in {"in", "out"}:
        raise ValidationError(f"未知资金方向：{direction}", field="direction")
    expected_direction = _DEFAULT_DIRECTION[transaction_type]
    if (
        transaction_type in {TransactionType.EXPENSE.value, TransactionType.INCOME.value}
        and direction != expected_direction
    ):
        # 支出却"增加余额"会让账户越记越多，且很难从界面上看出来
        raise ValidationError(f"{transaction_type} 的方向必须是 {expected_direction}", field="direction")

    account_id = resolved("account_id")
    if not account_id:
        raise ValidationError("必须指定账户", field="account_id")
    account = _require_account(session, int(account_id))

    to_account_id = resolved("to_account_id")
    if transaction_type == TransactionType.TRANSFER.value:
        if not to_account_id:
            raise ValidationError("转账必须指定目标账户", field="to_account_id")
        if int(to_account_id) == int(account_id):
            raise ValidationError("转出与转入账户不能相同", field="to_account_id")
        target = _require_account(session, int(to_account_id))
        if target.currency != account.currency:
            raise ValidationError("转账双方必须使用相同币种；暂不支持汇率换算", field="to_account_id")
    elif to_account_id:
        raise ValidationError("只有转账可以指定目标账户", field="to_account_id")

    category_id = resolved("category_id")
    if category_id:
        _require_category_for_type(session, int(category_id), transaction_type)
    elif transaction_type in _CATEGORY_KIND_FOR_TYPE and transaction_type != TransactionType.ADJUST.value:
        # 收支没有分类仍然允许（用户可能来不及选），但转账带分类没有意义
        pass

    status = str(resolved("status") or TransactionStatus.CLEARED.value)
    if status not in {member.value for member in TransactionStatus}:
        raise ValidationError(f"未知流水状态：{status}", field="status")

    occurred_at = resolved("occurred_at") or local_now()
    if isinstance(occurred_at, str):
        occurred_at = _parse_datetime(occurred_at)

    currency = str(resolved("currency") or account.currency or DEFAULT_CURRENCY)
    if currency != account.currency:
        raise ValidationError("流水币种必须与账户币种一致", field="currency")

    normalized: dict[str, Any] = {
        "type": transaction_type,
        "direction": direction,
        "occurred_at": occurred_at,
        "tz_offset_minutes": int(resolved("tz_offset_minutes") or 0),
        "account_id": int(account_id),
        "to_account_id": int(to_account_id) if to_account_id else None,
        "category_id": int(category_id) if category_id else None,
        "amount_minor": int(amount),
        "currency": currency,
        # P1 不做汇率换算：本位币金额恒等于原币金额。
        # 保留该列是为了将来接汇率时**不需要改表结构**。
        "base_amount_minor": int(amount),
        "status": status,
    }

    for passthrough in ("payee", "note", "source", "external_id", "project_id", "member_id", "meta"):
        if passthrough in fields:
            normalized[passthrough] = fields[passthrough]
        elif current is None and passthrough in {"payee", "note"}:
            normalized[passthrough] = ""

    if normalized.get("external_id") == "":
        # 空串会与唯一索引冲突（多个空串互不相等是 SQLite 的行为差异），统一转 None
        normalized["external_id"] = None

    return normalized


def _replace_tags(session: Session, transaction: Transaction, tag_ids: list[int]) -> None:
    """整体替换流水的标签。

    为什么要像分账一样"先完整校验、再改动状态"：
    传入的 id 里只要有一个不存在，就应该整笔操作失败，
    而不是"部分标签挂上了、部分没挂" —— 后者会让用户以为全部成功。
    """
    unique_ids = list(dict.fromkeys(int(item) for item in tag_ids))
    tags: list[Tag] = []
    for tag_id in unique_ids:
        tag = session.get(Tag, tag_id)
        if tag is None or tag.deleted_at is not None:
            raise NotFoundError("标签不存在或已被删除", entity="tag", entity_id=tag_id)
        tags.append(tag)

    # 原地替换内容而不是重新赋值：relationship 已被 selectin 加载，
    # 整体赋值在 SQLAlchemy 里会触发一次额外的 delete+insert，语义也更绕
    transaction.tags[:] = tags
    session.flush()


def _require_account(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None or account.is_deleted:
        raise NotFoundError("账户不存在或已被删除", entity="account", entity_id=account_id)
    return account


def _require_category_for_type(session: Session, category_id: int, transaction_type: str) -> Category:
    category = session.get(Category, category_id)
    if category is None or category.is_deleted:
        raise NotFoundError("分类不存在或已被删除", entity="category", entity_id=category_id)

    expected_kind = _CATEGORY_KIND_FOR_TYPE.get(transaction_type)
    if expected_kind is None:
        # 转账与校准不参与分类统计，带分类只会让人误以为它会出现在报表里
        raise ValidationError(
            f"{transaction_type} 流水不能指定收支分类", field="category_id", category_id=category_id
        )
    if category.kind != expected_kind:
        raise ValidationError(
            f"分类方向不匹配：{transaction_type} 流水只能用 {expected_kind} 分类",
            field="category_id",
            category_kind=category.kind,
            expected_kind=expected_kind,
        )
    return category


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"时间格式无法解析：{value}", field="occurred_at") from exc


def _replace_splits(session: Session, transaction: Transaction, splits: list[dict[str, Any]]) -> None:
    """整体替换分账并校验金额守恒。

    采用"整体替换"而不是逐条 diff：分账通常只有 2–5 条，
    整体替换的代码路径短得多，也就更不容易出错。

    实现要点：
        1. **先完整校验、再改动状态** —— 校验失败时不应留下半截修改；
        2. 删除旧分账用**清空关系集合**（配合 ``cascade="all, delete-orphan"``），
           而不是逐个 ``session.delete()``。后者只清理数据库，内存里的集合
           仍持有已删对象，调用方随后读到的是"删了但还在"的假象 ——
           这类不一致极难排查。
    """
    if splits:
        if len(splits) < 2:
            raise ValidationError("分账至少需要两条；只有一条时请直接使用流水分类", split_count=len(splits))
        if transaction.type in {item.value for item in TRANSFER_TYPES}:
            raise ValidationError("转账与校准流水不支持分账", field="splits")

        total = 0
        for index, item in enumerate(splits):
            amount = item.get("amount_minor")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise ValidationError("分账金额必须是正整数最小单位", index=index)
            if item.get("category_id"):
                _require_category_for_type(session, int(item["category_id"]), transaction.type)
            total += int(amount)

        if total != transaction.amount_minor:
            raise ValidationError(
                "分账金额之和必须等于流水金额",
                split_total_minor=total,
                amount_minor=transaction.amount_minor,
                difference_minor=transaction.amount_minor - total,
            )

    transaction.splits.clear()
    session.flush()

    for index, item in enumerate(splits or []):
        transaction.splits.append(
            TransactionSplit(
                category_id=int(item["category_id"]) if item.get("category_id") else None,
                amount_minor=int(item["amount_minor"]),
                note=str(item.get("note") or ""),
                sort_order=int(item.get("sort_order") or index),
            )
        )
    session.flush()


# -----------------------------------------------------------------------------
# 统计
# -----------------------------------------------------------------------------
def summary(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    account_ids: list[int] | None = None,
    top_categories: int = 0,
) -> SummaryResult:
    """区间收支汇总。

    **排除转账与校准**（``TRANSFER_TYPES``），否则"工资卡转支付宝"会被算成支出。
    作废（``void``）与已删除流水同样排除。
    """
    conditions = [
        Transaction.currency == DEFAULT_CURRENCY,
        Transaction.deleted_at.is_(None),
        Transaction.status != TransactionStatus.VOID.value,
        Transaction.type.not_in([item.value for item in TRANSFER_TYPES]),
    ]
    if start is not None:
        conditions.append(Transaction.occurred_at >= start)
    if end is not None:
        conditions.append(Transaction.occurred_at <= end)
    if account_ids:
        conditions.append(Transaction.account_id.in_(account_ids))

    rows = session.execute(
        select(
            Transaction.type,
            func.coalesce(func.sum(Transaction.amount_minor), 0),
            func.count(Transaction.id),
        )
        .where(and_(*conditions))
        .group_by(Transaction.type)
    ).all()

    result = SummaryResult()
    for transaction_type, total, count in rows:
        if transaction_type == TransactionType.INCOME.value:
            result.income_minor = int(total or 0)
        elif transaction_type == TransactionType.EXPENSE.value:
            result.expense_minor = int(total or 0)
        result.transaction_count += int(count or 0)

    if top_categories > 0:
        result.by_category = _category_breakdown(
            session,
            conditions=conditions,
            transaction_type=TransactionType.EXPENSE.value,
            limit=top_categories,
        )
    return result


def _category_breakdown(
    session: Session, *, conditions: list[Any], transaction_type: str, limit: int
) -> list[dict[str, Any]]:
    """按分类汇总。分账优先于主分类 —— 分账才是钱真正的去向。"""
    # 有分账的流水：金额按分账拆分统计
    split_rows = session.execute(
        select(
            TransactionSplit.category_id,
            func.coalesce(func.sum(TransactionSplit.amount_minor), 0),
        )
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(and_(*conditions), Transaction.type == transaction_type, Transaction.splits.any())
        .group_by(TransactionSplit.category_id)
    ).all()

    # 无分账的流水：按主分类统计
    plain_rows = session.execute(
        select(
            Transaction.category_id,
            func.coalesce(func.sum(Transaction.amount_minor), 0),
        )
        .where(and_(*conditions), Transaction.type == transaction_type, ~Transaction.splits.any())
        .group_by(Transaction.category_id)
    ).all()

    totals: dict[int | None, int] = {}
    for category_id, amount in [*split_rows, *plain_rows]:
        totals[category_id] = totals.get(category_id, 0) + int(amount or 0)

    names = dict(
        session.execute(
            select(Category.id, Category.name).where(Category.id.in_([k for k in totals if k]))
        ).all()
    )
    items = [
        {
            "category_id": category_id,
            "category_name": names.get(category_id, "未分类"),
            "amount_minor": amount,
        }
        for category_id, amount in totals.items()
    ]
    items.sort(key=lambda item: item["amount_minor"], reverse=True)
    return items[:limit]


def daily_totals(
    session: Session,
    *,
    start: date,
    end: date,
    include_transfers: bool = False,
) -> list[DailyTotal]:
    """按天聚合（日历热力图与时间轴视图的数据源）。

    刻意用 Python 侧补齐缺失的日期而不是在 SQL 里生成日期序列：
    前端需要"没记账的那天"也占位（否则日历会缺格），
    而 SQLite 生成日期序列不如显式补全易懂。
    """
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)
    conditions = [
        Transaction.currency == DEFAULT_CURRENCY,
        Transaction.deleted_at.is_(None),
        Transaction.status != TransactionStatus.VOID.value,
        Transaction.occurred_at >= start_dt,
        Transaction.occurred_at <= end_dt,
    ]
    if not include_transfers:
        conditions.append(Transaction.type.not_in([item.value for item in TRANSFER_TYPES]))

    rows = session.execute(
        select(
            func.date(Transaction.occurred_at),
            Transaction.type,
            func.coalesce(func.sum(Transaction.amount_minor), 0),
            func.count(Transaction.id),
        )
        .where(and_(*conditions))
        .group_by(func.date(Transaction.occurred_at), Transaction.type)
    ).all()

    buckets: dict[date, dict[str, int]] = {}
    for raw_day, transaction_type, total, count in rows:
        day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        bucket = buckets.setdefault(day, {"income": 0, "expense": 0, "count": 0})
        if transaction_type == TransactionType.INCOME.value:
            bucket["income"] += int(total or 0)
        elif transaction_type == TransactionType.EXPENSE.value:
            bucket["expense"] += int(total or 0)
        bucket["count"] += int(count or 0)

    totals: list[DailyTotal] = []
    cursor = start
    while cursor <= end:
        bucket = buckets.get(cursor, {"income": 0, "expense": 0, "count": 0})
        totals.append(
            DailyTotal(
                day=cursor,
                income_minor=bucket["income"],
                expense_minor=bucket["expense"],
                transaction_count=bucket["count"],
            )
        )
        cursor += timedelta(days=1)
    return totals


class _Unset:
    """区分"不改这个字段"与"把它清空"。

    用 ``None`` 当"不改"的哨兵是行不通的：``category_id=None`` 是一个
    合法且常见的意图（把分类清掉）。少了这个区分，批量操作要么改不动、
    要么会把用户没打算动的字段一起清掉。
    """

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "UNSET"


UNSET = _Unset()

#: 允许批量修改的字段。刻意不含 amount_minor / occurred_at / type：
#: 把一批金额不同的流水改成同一个金额，几乎总是误操作
BATCH_MUTABLE = ("category_id", "project_id", "member_id", "status")


def batch_update(
    session: Session,
    ids: list[int],
    *,
    category_id: Any = UNSET,
    project_id: Any = UNSET,
    member_id: Any = UNSET,
    status: Any = UNSET,
    tag_ids: list[int] | None = None,
    add_tag_ids: list[int] | None = None,
) -> dict[str, Any]:
    """批量修改流水。

    全是**加法与替换**、没有"清空全部标签"这种破坏性选项：
    批量操作的代价是用户看不清每一笔的后果，因此只提供意图明确的操作。

    返回结构里 ``skipped`` 列出被跳过的 id 与原因，而不是静默跳过 ——
    用户选中 10 笔只改了 8 笔时必须知道为什么。

    ``tag_ids`` 是**替换**，``add_tag_ids`` 是**追加**（批量打标签用），
    两者互斥。
    """
    if not ids:
        raise ValidationError("没有选中任何流水", field="ids")
    if len(ids) > 500:
        raise ValidationError("一次最多批量修改 500 笔", field="ids", max_items=500)

    changes: dict[str, Any] = {}
    # 循环变量不能叫 field —— 那会在函数作用域里遮蔽 `field` 这个导入
    for name, value in (
        ("category_id", category_id),
        ("project_id", project_id),
        ("member_id", member_id),
        ("status", status),
    ):
        if not isinstance(value, _Unset):
            changes[name] = value
    if not changes and tag_ids is None and not add_tag_ids:
        raise ValidationError("没有任何要修改的内容", field="changes")

    updated: list[int] = []
    skipped: list[dict[str, Any]] = []
    for transaction_id in ids:
        row = session.get(Transaction, transaction_id)
        if row is None or row.deleted_at is not None:
            skipped.append({"id": transaction_id, "reason": "not_found"})
            continue
        try:
            update_transaction(
                session,
                transaction_id,
                tag_ids=tag_ids,
                add_tag_ids=add_tag_ids,
                **changes,
            )
        except (NotFoundError, ValidationError, ConflictError) as error:
            # 单笔失败不该让整批回滚：用户已经选好了一批，
            # 因为其中一笔的分类被删掉而全部失败是很糟的体验
            skipped.append({"id": transaction_id, "reason": str(error)})
            continue
        updated.append(transaction_id)

    return {"updated": updated, "skipped": skipped, "count": len(updated)}


def batch_delete(session: Session, ids: list[int]) -> dict[str, Any]:
    """批量软删除（可恢复）。"""
    if not ids:
        raise ValidationError("没有选中任何流水", field="ids")
    if len(ids) > 500:
        raise ValidationError("一次最多批量删除 500 笔", field="ids", max_items=500)

    deleted: list[int] = []
    skipped: list[dict[str, Any]] = []
    for transaction_id in ids:
        row = session.get(Transaction, transaction_id)
        if row is None or row.deleted_at is not None:
            skipped.append({"id": transaction_id, "reason": "not_found"})
            continue
        delete_transaction(session, transaction_id)
        deleted.append(transaction_id)
    return {"deleted": deleted, "skipped": skipped, "count": len(deleted)}
