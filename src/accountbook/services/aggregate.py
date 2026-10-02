"""跨模块共用的事务聚合。

为什么单独抽出来
----------------
"某区间内按分类汇总的支出"这句话在预算、统计、K 线里都要用。
如果各写一份，早晚会出现"预算说超支了、统计页说没超"的情况 ——
而用户无从判断哪个对。因此这里只写一遍，并明确写死口径：

1. **转账与余额校准不参与**（``TRANSFER_TYPES``）——
   它们是资金搬运，不是收支；
2. **作废的流水不参与**（``VOID``）；
3. **软删除的不参与**；
4. **分账优先于主分类**：一笔流水拆成"午餐 70 + 打车 30"时，
   分类维度按 70/30 计，而不是按主分类记成一整笔。
   这是全项目统一的规则（见 docs/DATA_MODEL.md）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.domain import TransactionStatus, TransactionType
from ..core.money import DEFAULT_CURRENCY
from ..db.models import Transaction

__all__ = [
    "TRANSFER_TYPES",
    "category_totals",
    "expense_total",
    "load_transactions",
    "spending_by_day",
]

#: 不计入收支的流水类型。集中在这里定义，避免各处各写一份
TRANSFER_TYPES: frozenset[str] = frozenset({TransactionType.TRANSFER.value, TransactionType.ADJUST.value})


def load_transactions(
    session: Session,
    *,
    start: date,
    end: date,
    types: set[str] | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> list[Transaction]:
    """取出区间内**参与统计**的流水（已排除作废、软删除与转账）。

    ``occurred_at`` 是本地墙上时钟，因此左闭右闭的日期区间
    直接映射为 ``[start 00:00, end 23:59:59.999999]``。
    """
    statement = (
        select(Transaction)
        .options(selectinload(Transaction.splits))
        .where(
            Transaction.currency == currency,
            Transaction.deleted_at.is_(None),
            Transaction.status != TransactionStatus.VOID.value,
            Transaction.occurred_at >= datetime.combine(start, time.min),
            Transaction.occurred_at <= datetime.combine(end, time.max),
        )
        .order_by(Transaction.occurred_at, Transaction.id)
    )
    rows = list(session.scalars(statement).all())
    allowed = types if types is not None else None
    return [
        row for row in rows if row.type not in TRANSFER_TYPES and (allowed is None or row.type in allowed)
    ]


def category_totals(
    session: Session, *, start: date, end: date, kind: str = "expense"
) -> list[tuple[int | None, int]]:
    """按分类汇总金额，**分账优先**。返回按金额降序的 ``[(分类 id, 金额)]``。

    ``category_id`` 可能为 ``None``（未分类）—— 它是真实存在的一档，
    合并进"其它"会掩盖"我有多少笔没分类"，而那正是用户需要看到的。
    """
    totals: dict[int | None, int] = defaultdict(int)
    for transaction in load_transactions(session, start=start, end=end, types={kind}):
        if transaction.splits:
            for split in transaction.splits:
                totals[split.category_id] += split.amount_minor
        else:
            totals[transaction.category_id] += transaction.amount_minor
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)


def expense_total(
    session: Session,
    *,
    start: date,
    end: date,
    category_ids: set[int] | None = None,
    currency: str = DEFAULT_CURRENCY,
) -> int:
    """区间内的支出总额；给出 ``category_ids`` 时只统计这些分类（同样分账优先）。

    预算用它算"已用多少"。注意**不能**简单地对 ``category_totals`` 求和后
    按主分类过滤：一笔被拆到两个分类的流水，只应计入属于该分类的那一部分。
    """
    if category_ids is None:
        total = 0
        for transaction in load_transactions(
            session, start=start, end=end, types={"expense"}, currency=currency
        ):
            total += transaction.amount_minor
        return total

    total = 0
    for transaction in load_transactions(session, start=start, end=end, types={"expense"}, currency=currency):
        if transaction.splits:
            total += sum(
                split.amount_minor for split in transaction.splits if split.category_id in category_ids
            )
        elif transaction.category_id in category_ids:
            total += transaction.amount_minor
    return total


def spending_by_day(session: Session, *, start: date, end: date) -> dict[date, int]:
    """按日汇总支出，用于 K 线的成交量与预算的日均。"""
    buckets: dict[date, int] = defaultdict(int)
    for transaction in load_transactions(session, start=start, end=end, types={"expense"}):
        buckets[transaction.occurred_at.date()] += transaction.amount_minor
    return dict(buckets)
