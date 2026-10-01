"""日历与当日详情路由（REQ-20）。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...core.errors import ValidationError
from ...services import daily as daily_service
from ...services.stats import transaction_brief
from ..deps import get_session
from ..schemas import DayEventCreate, DayEventOut

__all__ = ["router"]

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

SessionDep = Depends(get_session)


@router.get("", summary="日历热力数据（含六类颜色指标）")
def calendar(
    start: date = Query(description="起始日期（含）"),
    end: date = Query(description="结束日期（含）"),
    metric: str = Query(
        default="entry", description="主指标：entry/expense/income/net/net_worth_change/anomaly"
    ),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """返回区间内**每一天**的数据，并给出图例与阈值说明。

    ``level`` 由服务端算好（分位数分级），目的是让日历、图例与导出 PNG
    使用同一套分级 —— 否则"看起来一样深的两格"含义会不同。
    指标本身与分级一起返回，前端不需要（也不应该）自己定阈值。
    """
    if end < start:
        raise ValidationError("结束日期不能早于起始日期", field="end")
    if (end - start).days > 366 * 3:
        raise ValidationError("一次最多查询 3 年", field="end", max_days=366 * 3)

    from ...services.categories import get_category

    cells = daily_service.get_calendar(session, start=start, end=end, metric=metric)  # type: ignore[arg-type]
    # 一次性查出所有出现过的分类名，避免逐格访问数据库
    names: dict[int, str] = {}
    for cell in cells:
        if cell.top_category_id and cell.top_category_id not in names:
            try:
                names[cell.top_category_id] = get_category(session, cell.top_category_id).name
            except Exception:  # noqa: BLE001 - 分类被删时不该让整页失败
                names[cell.top_category_id] = "未分类"

    return {
        "metric": metric if metric in daily_service.CALENDAR_METRICS else "entry",
        "metrics": list(daily_service.CALENDAR_METRICS),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": [
            {
                "date": cell.date.isoformat(),
                "income_minor": cell.income_minor,
                "expense_minor": cell.expense_minor,
                "net_minor": cell.net_minor,
                "net_worth_minor": cell.net_worth_minor,
                "tx_count": cell.tx_count,
                "pending_count": cell.pending_count,
                "entry_state": cell.entry_state,
                "anomaly_score": round(cell.anomaly_score, 4),
                "top_category_id": cell.top_category_id,
                "top_category_name": names.get(cell.top_category_id or 0, ""),
                "event_count": cell.event_count,
                "has_attachment": cell.has_attachment,
                "metric_value": round(cell.metric_value, 6),
                "level": cell.level,
                "badges": cell.badges,
            }
            for cell in cells
        ],
    }


@router.get("/day/{day}", summary="当日详情（余额阶梯曲线 / 构成 / 归因 / 事件）")
def day_detail(day: date, session: Session = SessionDep) -> dict[str, Any]:
    detail = daily_service.get_day_detail(session, day)
    detail["transactions"] = [transaction_brief(item) for item in detail["transactions"]]
    return detail


@router.post("/day/{day}/confirm", summary="标记某天为已核对")
def confirm_day(
    day: date,
    confirmed: bool = Query(default=True),
    session: Session = SessionDep,
) -> dict[str, Any]:
    daily_service.confirm_day(session, day, confirmed=confirmed)
    return {"date": day.isoformat(), "confirmed": confirmed}


@router.get("/net-worth", summary="按日净值序列")
def net_worth(
    start: date = Query(),
    end: date = Query(),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    return daily_service.net_worth_series(session, start=start, end=end)


# -----------------------------------------------------------------------------
# 事件日志
# -----------------------------------------------------------------------------
@router.get("/events", summary="按区间取事件日志")
def list_events(
    start: date = Query(),
    end: date = Query(),
    session: Session = SessionDep,
) -> list[DayEventOut]:
    from sqlalchemy import select

    from ...db.models import DayEvent

    rows = session.scalars(
        select(DayEvent)
        .where(DayEvent.deleted_at.is_(None), DayEvent.date.between(start, end))
        .order_by(DayEvent.date, DayEvent.sort_order, DayEvent.id)
    ).all()
    return [DayEventOut.model_validate(row) for row in rows]


@router.post(
    "/events",
    response_model=DayEventOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增事件",
)
def create_event(payload: DayEventCreate, session: Session = SessionDep) -> DayEventOut:
    event = daily_service.create_event(
        session,
        day=payload.date,
        kind=payload.kind,
        title=payload.title,
        body=payload.body,
        tags=payload.tags,
        attachments=payload.attachments,
    )
    return DayEventOut.model_validate(event)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除事件（软删除）")
def delete_event(event_id: int, session: Session = SessionDep) -> None:
    daily_service.delete_event(session, event_id)
