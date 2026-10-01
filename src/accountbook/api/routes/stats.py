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


@router.get("/category-trend", summary="按月的分类构成（堆叠面积图数据源）")
def category_trend(
    months: int = Query(default=12, ge=1, le=36),
    end: date | None = Query(default=None),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """按月的分类支出构成。

    月份**连续补齐**（没有支出的月份也会出现在 ``months`` 列表里）——
    堆叠面积图缺一个月会让横轴断开，看起来像数据丢了。
    """
    from ...services.categories import get_category
    from ...services.stats import category_trend as build_trend

    payload = build_trend(session, months=months, end=end)

    names: dict[int, str] = {}
    for row in payload["rows"]:
        category_id = row["category_id"]
        if category_id and category_id not in names:
            try:
                names[category_id] = get_category(session, category_id).name
            except Exception:  # noqa: BLE001 - 分类被删时不该让整页失败
                names[category_id] = "未分类"
    for row in payload["rows"]:
        row["category_name"] = names.get(row["category_id"] or 0, "未分类")

    return payload
