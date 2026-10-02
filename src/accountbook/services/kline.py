"""净值 K 线与技术指标（P3）。

口径必须先说清楚，因为它决定了图怎么读
--------------------------------------
每个周期的四个价都取自 ``daily_stats.net_worth_minor``（日终净值）：

* ``open`` = 周期**开始前一天**的日终净值。这样 K 线的实体表示
  "这个周期里净值涨跌了多少"，与股票的读法一致；
  如果 open 取周期第一天的日终值，那么"周一亏了钱"这件事在周线上会消失。
* ``high`` / ``low`` = 周期内日终净值的最大 / 最小值。
* ``close`` = 周期最后一天的日终净值。
* ``volume`` = 周期内**资金流动总额**（收入 + 支出）。它不是余额，
  回答的是"这个周期里有多少钱动过" —— 与 K 线图的成交量含义对应。

两个容易做错的地方
------------------
1. **指标必须带预热区间**。MA60 在区间的第 1 根就是"错的"（它用不到 60 个样本）。
   这与早前"异常分数基线算错基准"是同一类错误：数字看起来有，其实不对。
   因此本模块**总是多取** ``WARMUP_BARS`` 根再切片。
2. **当前周期永远是脏的**。今天的净值随时在变，缓存里的最后一根
   可能是一小时前算的。因此最后一个周期总是重算，不信任缓存。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core.domain import KlinePeriod
from ..core.errors import ValidationError
from ..core.periods import period_bounds, shift_period
from ..db.base import utc_now
from ..db.models import AssetOhlc, DailyStat, Transaction
from . import daily as daily_service

__all__ = [
    "DRAWDOWN_WINDOW",
    "MACD_PARAMS",
    "MA_WINDOWS",
    "RSI_PERIOD",
    "WARMUP_BARS",
    "bars",
    "ema",
    "invalidate",
    "macd",
    "moving_average",
    "rebuild",
    "rsi",
    "series",
]

_logger = logging.getLogger(__name__)

#: 均线窗口。选 5/10/20/60 是因为它们分别对应"一周/两周/一月/一季"
MA_WINDOWS: tuple[int, ...] = (5, 10, 20, 60)

#: MACD 参数（12, 26, 9）—— 这是通用约定，不做成可配置：
#: 让用户改参数只会让"和别处看到的 MACD 不一样"，没有实际收益
MACD_PARAMS: tuple[int, int, int] = (12, 26, 9)

#: RSI 周期（Wilder 原始参数）
RSI_PERIOD = 14

#: 回撤窗口。0 表示"从区间内最高点算起"
DRAWDOWN_WINDOW = 0

#: 指标预热所需的额外 K 线根数。取 60（最长均线）+ 26（MACD 慢线）+ 14（RSI）
#: 再留一倍余量，保证任何指标在区间第一根上都已经收敛
WARMUP_BARS = 200

#: 一次最多返回多少根。超过就自动换更粗的周期。
#: 400 的依据：横向 1200px 的图上，一根蜡烛约占 3px —— 再密就只剩一条色带，
#: 既读不出形状也点不中。
MAX_BARS = 400

#: 降周期的顺序。**不抽稀**：抽掉一根蜡烛，那个周期的开高低收就永久丢失了，
#: 画出来的"低点"可能比真实的低点高，用户会据此判断"那天没跌那么狠"。
#: 换更粗的周期则每一根都是真实的聚合结果，只是分辨率降低。
COARSEN_ORDER: dict[str, str] = {
    "day": "week",
    "week": "month",
    "month": "year",
    # year 已经是最粗的一档：真到那一步就只能截断，见 resolve_period
    "year": "year",
}

_PERIODS = {item.value for item in KlinePeriod}


# -----------------------------------------------------------------------------
# 纯指标计算（不碰数据库，可单独测试）
# -----------------------------------------------------------------------------
def moving_average(values: list[float], window: int) -> list[float | None]:
    """简单移动平均。样本不足时给 ``None``，**不给** 0 或缩短窗口的均值。

    缩短窗口会让"MA60"在开头变成"MA3"，图上看起来是连续的一条线，
    实际上是两种东西 —— 这种错误在图上完全看不出来，所以必须用 ``None``
    把"还不成立"明确表达出来。
    """
    if window <= 0:
        raise ValidationError("均线窗口必须大于 0", field="window")
    result: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        result.append(running / window if index >= window - 1 else None)
    return result


def ema(values: list[float], window: int) -> list[float]:
    """指数移动平均。第一个样本作为种子。

    种子方式会影响开头若干根的数值，因此指标一律在**带预热区间**上计算，
    预热长度远大于收敛所需，这样切片后的前几根已经稳定。
    """
    if window <= 0:
        raise ValidationError("EMA 窗口必须大于 0", field="window")
    if not values:
        return []
    alpha = 2 / (window + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def macd(values: list[float], *, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float]]:
    """MACD。

    ``histogram`` 用 ``2 * (dif - dea)``：这是国内行情软件的画法，
    与用户在其他地方看到的一致。用 ``dif - dea`` 也能算，但柱子高度会差一倍，
    用户会以为两个软件里有一个是错的。
    """
    if not values:
        return {"dif": [], "dea": [], "histogram": []}
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    dif = [a - b for a, b in zip(fast_line, slow_line, strict=True)]
    dea = ema(dif, signal)
    histogram = [2 * (a - b) for a, b in zip(dif, dea, strict=True)]
    return {"dif": dif, "dea": dea, "histogram": histogram}


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """RSI（Wilder 平滑）。

    全程无涨跌时返回 50（中性），而不是 0 或 100 ——
    "完全没变化"既不是超买也不是超卖，编一个极值会误导。
    """
    if len(values) < 2:
        return [None] * len(values)
    result: list[float | None] = [None] * len(values)
    gains = 0.0
    losses = 0.0
    average_gain = 0.0
    average_loss = 0.0
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(0.0, change)
        loss = max(0.0, -change)
        if index <= period:
            gains += gain
            losses += loss
            if index == period:
                average_gain = gains / period
                average_loss = losses / period
                result[index] = _rsi_from(average_gain, average_loss)
            continue
        # Wilder 平滑：不是简单平均，权重不同，因此不能用移动平均代替
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        result[index] = _rsi_from(average_gain, average_loss)
    return result


def _rsi_from(average_gain: float, average_loss: float) -> float:
    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def drawdown(values: list[float]) -> list[float]:
    """从区间内历史最高点回落的幅度（负数或 0）。

    用**历史最高点**而不是"上一根"，否则每次小回调都会被算成回撤，
    图上会是一条锯齿，读不出真正的深度。
    """
    result: list[float] = []
    peak: float | None = None
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak in (None, 0):
            result.append(0.0)
        else:
            result.append((value - peak) / abs(peak))
    return result


# -----------------------------------------------------------------------------
# 周期与缓存
# -----------------------------------------------------------------------------
def _period_range(period: str, start: date, end: date) -> list[tuple[date, date]]:
    """把 ``[start, end]`` 切成该周期下的起止对（含首尾，可能被裁到边界）。"""
    if period not in _PERIODS:
        raise ValidationError(f"未知 K 线周期：{period}", field="period")
    if period == KlinePeriod.DAY.value:
        days: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            days.append((cursor, cursor))
            cursor += timedelta(days=1)
        return days

    spans: list[tuple[date, date]] = []
    anchor = start
    while anchor <= end:
        bounds = period_bounds(period, anchor)
        # 裁到请求边界：否则最后一根会包含区间之外的日子，数字对不上
        spans.append((max(bounds[0], start), min(bounds[1], end)))
        anchor = period_bounds(period, anchor)[1] + timedelta(days=1)
    return spans


def invalidate(session: Session, start: date, end: date) -> int:
    """删除与 ``[start, end]`` 重叠的 OHLC 缓存行。

    由日结重算调用（与 ``daily_stats`` 共用一套脏标记）。
    这里**不重算**，只作废 —— 下次读取自然重算，避免一次改动触发大量计算。
    """
    result = session.execute(
        delete(AssetOhlc).where(AssetOhlc.period_end >= start, AssetOhlc.period_start <= end)
    )
    return int(result.rowcount or 0)


def _first_record_day(session: Session) -> date | None:
    """账本里第一笔流水的日期；完全没有流水时返回 ``None``。

    "没有数据"与"数据是 0"必须区分开：前者不该画出蜡烛，
    后者要如实画一根 0。日结缓存会把区间内每一天都补齐（没有流水的那天
    净值不变），因此在账本开始之前也会有一串 0 —— 直接画出来等于告诉用户
    "那段时间净值是 0"，那是一句假话。
    """
    value = session.scalar(select(func.min(Transaction.occurred_at)))
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def rebuild(session: Session, *, period: str, start: date, end: date) -> int:
    """重算并写入 ``[start, end]`` 的 OHLC。返回写入的根数。"""
    spans = _period_range(period, start, end)
    if not spans:
        return 0

    # 日结缓存先保证新鲜 —— K 线是它的聚合，它不新鲜 K 线一定不对
    daily_service.ensure_fresh(session, spans[0][0] - timedelta(days=1), spans[-1][1])

    # 账本开始之前的周期不生成蜡烛（理由见 _first_record_day）
    first_day = _first_record_day(session)
    if first_day is None:
        return 0
    spans = [span for span in spans if span[1] >= first_day]
    if not spans:
        return 0

    # open 需要"周期开始前一天"的净值，因此把查询窗口向前扩一天
    query_start = spans[0][0] - timedelta(days=1)
    rows = session.execute(
        select(
            DailyStat.date,
            DailyStat.net_worth_minor,
            DailyStat.income_minor,
            DailyStat.expense_minor,
            DailyStat.tx_count,
        )
        .where(DailyStat.date >= query_start, DailyStat.date <= end)
        .order_by(DailyStat.date)
    ).all()
    by_day = {
        (row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))): {
            "net_worth": int(row[1] or 0),
            "flow": int(row[2] or 0) + int(row[3] or 0),
            "tx_count": int(row[4] or 0),
        }
        for row in rows
    }

    # 一次读出已有行，避免逐根 SELECT
    existing = {
        row.period_start: row
        for row in session.scalars(
            select(AssetOhlc).where(
                AssetOhlc.period == period,
                AssetOhlc.period_start.in_([span[0] for span in spans]),
            )
        ).all()
    }

    written = 0
    for span_start, span_end in spans:
        # 逐日取净值序列（缺口用"最近一个已知值"补齐：没有流水的日子净值不变，
        # 而不是 0。用 0 会让 high/low 出现断崖）
        series: list[int] = []
        cursor = span_start
        last_known: int | None = None
        carry = by_day.get(span_start - timedelta(days=1))
        if carry is not None:
            last_known = carry["net_worth"]
        while cursor <= span_end:
            bucket = by_day.get(cursor)
            if bucket is not None:
                last_known = bucket["net_worth"]
            if last_known is not None:
                series.append(last_known)
            cursor += timedelta(days=1)

        if not series and last_known is None:
            # 这个周期完全没有数据（在账本开始之前）：跳过而不是写一根 0
            continue

        opening = by_day.get(span_start - timedelta(days=1), {}).get("net_worth")
        if opening is None:
            opening = series[0] if series else 0

        flow = 0
        tx_count = 0
        cursor = span_start
        while cursor <= span_end:
            bucket = by_day.get(cursor)
            if bucket is not None:
                flow += bucket["flow"]
                tx_count += bucket["tx_count"]
            cursor += timedelta(days=1)

        row = existing.get(span_start)
        if row is None:
            row = AssetOhlc(period=period, period_start=span_start, period_end=span_end)
            session.add(row)
        row.period_end = span_end
        row.open_minor = int(opening)
        row.close_minor = int(series[-1] if series else opening)
        row.high_minor = int(max([*series, opening])) if series else int(opening)
        row.low_minor = int(min([*series, opening])) if series else int(opening)
        row.volume_minor = flow
        row.tx_count = tx_count
        row.computed_at = utc_now()
        written += 1

    session.flush()
    return written


def bars(
    session: Session, *, period: str, start: date, end: date, warmup: int = WARMUP_BARS
) -> list[dict[str, Any]]:
    """取 OHLC 序列，并把缓存补齐。

    返回值**只包含请求区间内的根**；预热根在内部使用后即被丢弃
    （见 ``series`` 的做法）。
    """
    spans = _period_range(period, start, end)
    if not spans:
        return []

    # **先**保证日结新鲜。顺序很关键：
    #
    # 日结一变会顺带作废 OHLC（见 daily.recompute_range），但作废只在
    # 重算发生时才会发生。如果先查缓存、只在"缺行"时才重建，
    # 那么"缓存行还在、但底下的日结已经脏了"这种情况就会**直接返回旧值** ——
    # 用户记完一笔账，K 线的最后一根纹丝不动。
    # 曾经就是这么写的，属于"看起来能跑、数据是错的"那一类。
    daily_service.ensure_fresh(session, spans[0][0], spans[-1][1])

    # 账本开始之前的周期不生成蜡烛（理由见 rebuild 内的说明）
    first_day = _first_record_day(session)
    if first_day is None:
        # 一笔流水都没有：K 线图没有可画的东西，返回空数组而不是一串 0
        return []
    spans = [span for span in spans if span[1] >= first_day]
    if not spans:
        return []

    # 当前周期永远视为脏：今天的净值随时在变
    today = date.today()
    existing = {
        row.period_start: row
        for row in session.scalars(
            select(AssetOhlc).where(
                AssetOhlc.period == period, AssetOhlc.period_start.in_([s[0] for s in spans])
            )
        ).all()
    }
    needs_rebuild = any(
        span_start not in existing or span_start == period_bounds(period, today)[0] for span_start, _ in spans
    )
    if needs_rebuild:
        rebuild(session, period=period, start=start, end=end)

    rows = session.scalars(
        select(AssetOhlc)
        .where(AssetOhlc.period == period, AssetOhlc.period_start >= start, AssetOhlc.period_start <= end)
        .order_by(AssetOhlc.period_start)
    ).all()
    return [
        {
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
            "open_minor": row.open_minor,
            "high_minor": row.high_minor,
            "low_minor": row.low_minor,
            "close_minor": row.close_minor,
            "volume_minor": row.volume_minor,
            "tx_count": row.tx_count,
        }
        for row in rows
    ]


def resolve_period(period: str, start: date, end: date) -> tuple[str, bool]:
    """按根的密度决定实际使用的周期。

    返回 ``(实际周期, 是否被降过)``。降级的理由必须能回到用户面前 ——
    界面上要明说"区间太长，已按月线显示"，否则他会以为自己在看日线。
    """
    if period not in _PERIODS:
        raise ValidationError(f"未知 K 线周期：{period}", field="period")

    resolved = period
    while resolved != "year":
        spans = _period_range(resolved, start, end)
        if len(spans) <= MAX_BARS:
            break
        resolved = COARSEN_ORDER[resolved]
    return resolved, resolved != period


def _warmup_start(period: str, start: date, warmup: int) -> date:
    """把起点往前推 ``warmup`` 个周期。

    这是本模块最重要的一行：指标在区间第 1 根上的值取决于**区间之前**的数据。
    不预热就等于"每个月的前几天 MA60 都是错的"，而图上完全看不出来。
    """
    if warmup <= 0:
        return start
    return shift_period(period, start, -warmup)


def series(
    session: Session,
    *,
    period: str,
    start: date,
    end: date,
    indicators: bool = True,
    warmup: int = WARMUP_BARS,
) -> dict[str, Any]:
    """K 线数据 + 技术指标。

    指标在**预热后的长序列**上计算，再切回请求区间 —— 因此区间第一根上的
    MA60/MACD/RSI 都是已经收敛的值，而不是"从零开始算的假值"。
    """
    if end < start:
        raise ValidationError("结束日期不能早于起始日期", field="end")
    if period not in _PERIODS:
        raise ValidationError(f"未知 K 线周期：{period}", field="period")

    # 长区间自动降周期（见 MAX_BARS 的说明）。指标在此基础上计算，
    # 因此降级后的 MA/MACD 仍然是"该周期下正确的那条"。
    # 变量名不能叫 requested —— 本函数下面已经用它表示"切片后的蜡烛数组"，
    # 重名会让这个字符串在返回前被那串数组覆盖掉（这个坑真实踩过一次）
    requested_period = period
    resolved, downsampled = resolve_period(period, start, end)
    period = resolved

    extended_start = _warmup_start(period, start, warmup) if indicators else start
    extended = bars(session, period=period, start=extended_start, end=end)

    closes = [row["close_minor"] / 100 for row in extended]
    computed: dict[str, Any] = {
        "ma": {str(window): moving_average(closes, window) for window in MA_WINDOWS},
        "macd": macd(closes, fast=MACD_PARAMS[0], slow=MACD_PARAMS[1], signal=MACD_PARAMS[2]),
        "rsi": rsi(closes, RSI_PERIOD),
        "drawdown": drawdown(closes),
    }

    # 切回请求区间：按 period_start 找到第一根落在请求范围内的 K 线。
    # 不能靠 len(extended) - len(再查一次)，那会多查一次库，
    # 而且两次查询之间数据可能已经变化，切出来的边界会错位。
    start_key = start.isoformat()
    offset = next(
        (index for index, row in enumerate(extended) if row["period_start"] >= start_key),
        len(extended),
    )
    requested = extended[offset:]

    def _slice(values: list[Any]) -> list[Any]:
        return values[offset : offset + len(requested)]

    # 切片只在循环外做一次。放进推导式里会对每一根都重算整段切片（O(n²)）
    ma_series = {str(window): _slice(computed["ma"][str(window)]) for window in MA_WINDOWS}
    dif_series = _slice(computed["macd"]["dif"]) if indicators else []
    dea_series = _slice(computed["macd"]["dea"]) if indicators else []
    macd_series = _slice(computed["macd"]["histogram"]) if indicators else []
    rsi_series = _slice(computed["rsi"]) if indicators else []
    drawdown_series = _slice(computed["drawdown"]) if indicators else []

    payload = [
        {
            **row,
            # 指标随根返回，前端不需要（也不应该）自己算 ——
            # 两处实现必然会有一次不一致，而用户无从判断哪个对
            "ma": {str(window): ma_series[str(window)][index] for window in MA_WINDOWS},
            "dif": dif_series[index] if indicators else None,
            "dea": dea_series[index] if indicators else None,
            "macd": macd_series[index] if indicators else None,
            "rsi": rsi_series[index] if indicators else None,
            "drawdown": drawdown_series[index] if indicators else None,
        }
        for index, row in enumerate(requested)
    ]

    return {
        "period": period,
        # 用户要的周期与实际用的周期分开返回：界面据此提示"已自动降级"。
        # 只给一个 period 的话，前端无法区分"本来就要月线"与"被降级了"
        "requested_period": requested_period,
        "downsampled": downsampled,
        "max_bars": MAX_BARS,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "warmup_bars": warmup if indicators else 0,
        "params": {
            "ma_windows": list(MA_WINDOWS),
            "macd": {"fast": MACD_PARAMS[0], "slow": MACD_PARAMS[1], "signal": MACD_PARAMS[2]},
            "rsi_period": RSI_PERIOD,
        },
        "bars": payload,
        "count": len(payload),
    }
