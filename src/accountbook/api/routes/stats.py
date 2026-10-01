"""统计路由 —— 仪表盘与数据分析的数据源。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...services import stats as stats_service
from ...services import transactions as transactions_service
from ..deps import get_session

__all__ = ["router"]

router = APIRouter(prefix="/api/stats", tags=["stats"])

SessionDep = Depends(get_session)


@router.get("/dashboard", summary="仪表盘聚合数据")
def dashboard(
    reference: date | None = Query(default=None, description="参考日期（默认今天），便于查看历史某天"),
    trend_days: int = Query(default=30, ge=7, le=366),
    recent_limit: int = Query(default=8, ge=1, le=50),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """一次返回首页所需的全部数字（净值、本月收支、环比、趋势、最近流水）。"""
    return stats_service.dashboard(
        session, reference=reference, trend_days=trend_days, recent_limit=recent_limit
    )


@router.get("/cash-flow", summary="按月现金流趋势")
def cash_flow(
    months: int = Query(default=12, ge=1, le=60),
    reference: date | None = Query(default=None),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    return stats_service.cash_flow_trend(session, months=months, reference=reference)


@router.get("/calendar", summary="日历热力数据（按天聚合）")
def calendar(
    start: date = Query(description="起始日期（含）"),
    end: date = Query(description="结束日期（含）"),
    include_transfers: bool = Query(default=False),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    """返回区间内**每一天**的数据（没有记账的日子也会占位）。

    占位是有意的：日历缺少格子会让用户以为应用坏了，
    而"这天没记账"本身就是要展示的信息（REQ-20 的登记状态指标）。
    """
    totals = transactions_service.daily_totals(
        session, start=start, end=end, include_transfers=include_transfers
    )
    return [
        {
            "date": item.day.isoformat(),
            "income_minor": item.income_minor,
            "expense_minor": item.expense_minor,
            "net_minor": item.income_minor - item.expense_minor,
            "transaction_count": item.transaction_count,
            "has_entries": item.transaction_count > 0,
        }
        for item in totals
    ]


@router.get("/summary", summary="任意区间收支汇总")
def summary(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    top_categories: int = Query(default=0, ge=0, le=50),
    session: Session = SessionDep,
) -> dict[str, Any]:
    from datetime import datetime, time

    result = transactions_service.summary(
        session,
        start=datetime.combine(start, time.min) if start else None,
        end=datetime.combine(end, time.max) if end else None,
        top_categories=top_categories,
    )
    return {
        "income_minor": result.income_minor,
        "expense_minor": result.expense_minor,
        "net_minor": result.net_minor,
        "transaction_count": result.transaction_count,
        "by_category": result.by_category,
    }


@router.get("/integrity", summary="数据体检")
def integrity(session: Session = SessionDep) -> dict[str, Any]:
    """检查"数据悄悄坏了"的情况（悬空引用、分账金额不符等）。"""
    return stats_service.integrity_report(session)
