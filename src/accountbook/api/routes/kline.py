"""净值 K 线路由（P3）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core.domain import KlinePeriod
from ...core.errors import ValidationError
from ...services import kline as kline_service
from ..deps import get_session

__all__ = ["router"]

router = APIRouter(prefix="/api/kline", tags=["kline"])

SessionDep = Depends(get_session)

#: 各周期的默认回看长度。给默认值是必要的：让用户每次先选起止日期
#: 才能看到图，是把配置负担推给了使用者。想看更早就自己改。
DEFAULT_SPAN_DAYS = {
    KlinePeriod.DAY.value: 90,
    KlinePeriod.WEEK.value: 365,
    KlinePeriod.MONTH.value: 365 * 3,
    KlinePeriod.YEAR.value: 365 * 10,
}


@router.get("", summary="净值 K 线（含 MA / MACD / RSI / 回撤）")
def kline(
    period: str = Query(default="day", description="day/week/month/year"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    indicators: bool = Query(default=True),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """K 线数据。

    **指标由服务端算好**（并且带预热区间）。让前端自己算 MA/MACD 的话，
    这一页的 MACD 和任何其它地方的 MACD 迟早会不一样，
    而用户无从判断哪个对。
    """
    if period not in DEFAULT_SPAN_DAYS:
        raise ValidationError(f"未知 K 线周期：{period}", field="period")

    today = date.today()
    resolved_end = end or today
    resolved_start = start or (resolved_end - timedelta(days=DEFAULT_SPAN_DAYS[period]))
    if resolved_end < resolved_start:
        raise ValidationError("结束日期不能早于起始日期", field="end")
    # 长区间**不再拒绝**，而是由服务层自动换更粗的周期（见 services/kline.py
    # 的 MAX_BARS）。原来硬性拒绝日线超过 5 年 —— 那会挡掉本可以服务的请求，
    # 而"蜡烛太密看不清"完全可以用降级解决，不该变成一个错误。
    # 这里只兜底一个荒谬的上限，避免有人查询公元 1000 年至今。
    if (resolved_end - resolved_start).days > 365 * 100:
        raise ValidationError("一次最多查询 100 年", field="end", max_days=365 * 100)

    return kline_service.series(
        session,
        period=period,
        start=resolved_start,
        end=resolved_end,
        indicators=indicators,
    )


@router.get("/params", summary="当前使用的指标参数（供口径说明页展示）")
def params() -> dict[str, Any]:
    """把参数作为数据暴露出来，而不是写死在文档里。

    口径说明页直接渲染这个接口 —— 参数改了说明页自动跟着变，
    不会出现"文档说 MA20、代码里是 MA25"这种最难发现的不一致。
    """
    return {
        "ma_windows": list(kline_service.MA_WINDOWS),
        "macd": {
            "fast": kline_service.MACD_PARAMS[0],
            "slow": kline_service.MACD_PARAMS[1],
            "signal": kline_service.MACD_PARAMS[2],
        },
        "rsi_period": kline_service.RSI_PERIOD,
        "warmup_bars": kline_service.WARMUP_BARS,
        "periods": list(DEFAULT_SPAN_DAYS),
    }
