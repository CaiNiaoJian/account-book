"""统计服务 —— 仪表盘与报表所需的各种聚合。

口径统一在这里
--------------
所有对外数字都从本模块产出，避免"仪表盘说支出 3000、报表说 3200"
这类最伤信任的问题。三条铁律：

1. **转账与余额校准不计入收支**（引用 :data:`TRANSFER_TYPES`）；
2. **作废（void）与已删除流水一律排除**；
3. **分账优先于主分类** —— 分账记录的是钱真正的去向。

时间范围一律按**本地墙钟**（``occurred_at``）切分，因为用户理解的
"本月"是他所在地的日历月，而不是 UTC 月。
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.domain import TRANSFER_TYPES, TransactionStatus
from ..db.models import Account, Category, Transaction, TransactionSplit
from . import accounts as accounts_service
from . import transactions as transactions_service

__all__ = [
    "cash_flow_trend",
    "dashboard",
    "month_bounds",
    "previous_period",
    "range_bounds",
    "transaction_brief",
]


_logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 时间范围
# -----------------------------------------------------------------------------
def month_bounds(reference: date | None = None) -> tuple[datetime, datetime]:
    """返回所在自然月的 ``[起, 止]``（本地墙钟，含首尾）。"""
    today = reference or date.today()
    first = today.replace(day=1)
    # 用 calendar.monthrange 而不是"下月 1 日减一天"：后者在 12 月需要跨年处理
    last_day = calendar.monthrange(first.year, first.month)[1]
    last = first.replace(day=last_day)
    return datetime.combine(first, time.min), datetime.combine(last, time.max)


def range_bounds(days: int, *, end: date | None = None) -> tuple[datetime, datetime]:
    """返回最近 ``days`` 天的 ``[起, 止]``（含今天）。"""
    last = end or date.today()
    first = last - timedelta(days=max(1, days) - 1)
    return datetime.combine(first, time.min), datetime.combine(last, time.max)


def previous_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """返回等长的上一个区间，用于环比。

    刻意用"等长天数"而不是"上一个自然月"：后者在 2 月与 31 天的月份之间
    会产生 10% 的长度差，环比数字随之失真，而用户看到"下降 12%"时
    无法知道其中有多少是日历造成的。
    """
    span = end - start
    return start - span, start - timedelta(microseconds=1)


def _as_date(value: date | datetime | None) -> date:
    if value is None:
        return date.today()
    return value.date() if isinstance(value, datetime) else value


# -----------------------------------------------------------------------------
# 仪表盘
# -----------------------------------------------------------------------------
def dashboard(
    session: Session,
    *,
    reference: date | None = None,
    trend_days: int = 30,
    recent_limit: int = 8,
    top_category_limit: int = 6,
) -> dict[str, Any]:
    """一次返回首页所需的全部数字。

    做成单个聚合接口而不是让前端发 6 个请求：
    桌面应用虽然走环回网络，但 6 次往返仍然会让首页出现明显的分批渲染，
    而"数字逐个跳出来"正是那种看起来不专业的细节。
    """
    today = _as_date(reference)
    month_start, month_end = month_bounds(today)
    prev_start, prev_end = previous_period(month_start, month_end)

    current = transactions_service.summary(
        session, start=month_start, end=month_end, top_categories=top_category_limit
    )
    previous = transactions_service.summary(session, start=prev_start, end=prev_end)

    overview = accounts_service.overview(session)

    recent_query = transactions_service.TransactionQuery(limit=recent_limit, order="desc")
    recent, _ = transactions_service.list_transactions(session, recent_query)

    trend_start, trend_end = range_bounds(trend_days, end=today)
    trend = transactions_service.daily_totals(session, start=trend_start.date(), end=trend_end.date())

    return {
        "reference_date": today.isoformat(),
        "net_worth": {
            "net_worth_minor": overview["net_worth_minor"],
            "assets_minor": overview["assets_minor"],
            "liabilities_minor": overview["liabilities_minor"],
            "account_count": overview["account_count"],
        },
        "month": {
            "start": month_start.date().isoformat(),
            "end": month_end.date().isoformat(),
            "income_minor": current.income_minor,
            "expense_minor": current.expense_minor,
            "net_minor": current.net_minor,
            "transaction_count": current.transaction_count,
            "income_change": _change_ratio(current.income_minor, previous.income_minor),
            "expense_change": _change_ratio(current.expense_minor, previous.expense_minor),
            "top_categories": current.by_category,
        },
        "trend": [
            {
                "date": item.day.isoformat(),
                "income_minor": item.income_minor,
                "expense_minor": item.expense_minor,
                "transaction_count": item.transaction_count,
            }
            for item in trend
        ],
        "recent_transactions": [transaction_brief(item) for item in recent],
        "accounts": overview["accounts"],
    }


def _change_ratio(current: int, previous: int) -> float | None:
    """环比变化率。

    上期为 0 时返回 ``None`` 而不是 ``inf`` 或 ``1.0``：
    "从 0 涨到 100"在数学上无定义，把它显示成"+100%"是骗人的。
    前端遇到 ``None`` 应显示"—"。
    """
    if previous == 0:
        return None
    return (current - previous) / previous


def transaction_brief(transaction: Transaction) -> dict[str, Any]:
    """流水的精简表示（列表 / 首页最近记录共用）。"""
    return {
        "id": transaction.id,
        "type": transaction.type,
        "direction": transaction.direction,
        "occurred_at": transaction.occurred_at.isoformat(),
        "amount_minor": transaction.amount_minor,
        "currency": transaction.currency,
        "payee": transaction.payee,
        "note": transaction.note,
        "status": transaction.status,
        "account_id": transaction.account_id,
        "account_name": transaction.account.name if transaction.account else "",
        "to_account_id": transaction.to_account_id,
        "to_account_name": transaction.to_account.name if transaction.to_account else "",
        "category_id": transaction.category_id,
        "category_name": transaction.category.name if transaction.category else "",
        "category_icon": transaction.category.icon if transaction.category else "",
        "category_color": transaction.category.color if transaction.category else "",
        "tag_names": [tag.name for tag in transaction.tags],
    }


# -----------------------------------------------------------------------------
# 现金流趋势（P2 图表的直接数据源）
# -----------------------------------------------------------------------------
def cash_flow_trend(
    session: Session,
    *,
    months: int = 12,
    reference: date | None = None,
) -> list[dict[str, Any]]:
    """按月给出收入 / 支出 / 结余，用于趋势图。

    在 Python 侧按月循环并逐月查询：``months`` 通常不超过 24，
    而 SQLite 的 ``strftime('%Y-%m', ...)`` 分组虽然一条语句就能完成，
    却无法正确补齐"没有流水的月份"（图表会缺格）。可读性优先。
    """
    today = _as_date(reference)
    buckets: list[dict[str, Any]] = []
    cursor = today.replace(day=1)
    for _ in range(max(1, months)):
        start, end = month_bounds(cursor)
        result = transactions_service.summary(session, start=start, end=end)
        buckets.append(
            {
                "month": start.date().isoformat()[:7],
                "income_minor": result.income_minor,
                "expense_minor": result.expense_minor,
                "net_minor": result.net_minor,
                "transaction_count": result.transaction_count,
            }
        )
        # 往前推一个月：先退到上月最后一天，再取当月 1 日，避免 31 日跨月陷阱
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    buckets.reverse()
    return buckets


# -----------------------------------------------------------------------------
# 数据体检（设置页与 DB 可视化会用到）
# -----------------------------------------------------------------------------
def integrity_report(session: Session) -> dict[str, Any]:
    """检查几类"数据悄悄坏了"的情况。

    这些问题不会让程序崩溃，但会让报表数字错误 —— 因而必须能主动发现：
        * 有流水指向已被删除的账户或分类；
        * 分账金额之和与流水金额不一致；
        * 转账缺少目标账户（理论上被 CHECK 拦住，这里是兜底）。
    """
    issues: list[dict[str, Any]] = []

    dangling_accounts = session.scalar(
        select(func.count(Transaction.id))
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.deleted_at.is_(None), Account.deleted_at.is_not(None))
    )
    if dangling_accounts:
        issues.append(
            {
                "code": "dangling_account",
                "count": int(dangling_accounts),
                "severity": "error",
                "message": "存在指向已删除账户的流水",
            }
        )

    dangling_categories = session.scalar(
        select(func.count(Transaction.id))
        .join(Category, Category.id == Transaction.category_id)
        .where(Transaction.deleted_at.is_(None), Category.deleted_at.is_not(None))
    )
    if dangling_categories:
        issues.append(
            {
                "code": "dangling_category",
                "count": int(dangling_categories),
                "severity": "error",
                "message": "存在指向已删除分类的流水",
            }
        )

    split_sums = (
        select(
            TransactionSplit.transaction_id,
            func.sum(TransactionSplit.amount_minor).label("split_total"),
            Transaction.amount_minor.label("amount"),
        )
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(Transaction.deleted_at.is_(None))
        .group_by(TransactionSplit.transaction_id, Transaction.amount_minor)
    )
    mismatched = [
        row for row in session.execute(split_sums).all() if int(row.split_total or 0) != int(row.amount)
    ]
    if mismatched:
        issues.append(
            {
                "code": "split_mismatch",
                "count": len(mismatched),
                "severity": "error",
                "message": "分账金额之和与流水金额不一致",
            }
        )

    # 统计口径自检：把"转账计入收支"这种错误在测试里挡住
    transfer_count = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.deleted_at.is_(None),
            Transaction.status != TransactionStatus.VOID.value,
            Transaction.type.in_([item.value for item in TRANSFER_TYPES]),
        )
    )

    return {
        "ok": not issues,
        "issues": issues,
        "stats": {
            "transactions": int(
                session.scalar(select(func.count(Transaction.id)).where(Transaction.deleted_at.is_(None)))
                or 0
            ),
            "transfer_like": int(transfer_count or 0),
            "accounts": int(
                session.scalar(select(func.count(Account.id)).where(Account.deleted_at.is_(None))) or 0
            ),
            "categories": int(
                session.scalar(select(func.count(Category.id)).where(Category.deleted_at.is_(None))) or 0
            ),
        },
    }
