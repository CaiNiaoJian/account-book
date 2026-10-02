"""日结聚合与日历指标 —— P2 所有日粒度可视化的数据源。

为什么需要缓存而不是每次聚合
----------------------------
1. GitHub 式日历一次要画 **371 天**（53 周 × 7 天）。逐日聚合流水 = 371 次查询；
2. ``anomaly_score``（与同星期历史基线的偏离）必须看历史，无法从单日流水推出；
3. ``top_category_id`` 要遵守"分账优先"的口径，放在聚合里算一次即可；
4. 净值曲线需要"每天收盘余额"，而流水可被回填到任意历史日期 ——
   这意味着历史某天的余额会变，必须能**成区间重算**。

权威性与一致性
--------------
**流水是唯一事实来源，``daily_stats`` / ``asset_snapshots`` 只是派生缓存。**
因此本模块采用"脏标记 + 读取时懒重算"：

* 任何流水或账户变更都会调用 :func:`mark_dirty_from`，记下受影响的起始日；
* 任何读取都会先调用 :func:`ensure_fresh`，把脏区间补齐。

这样正确性**不依赖调用方记得刷新**。忘记标脏的代价只是多算一次，
忘记重算的代价是显示错数据 —— 两者严重程度差得远，所以这里偏保守。

派生量不重复存储
----------------
"当日净值变化率"完全可由相邻两天的 ``net_worth_minor`` 推出，因此**不落库**，
在读取时从序列现算。把每个派生量都存一份的做法会让缓存表越来越宽，
而每一列都是一次"忘了同步就出错"的机会。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any, Literal

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from ..core.domain import TRANSFER_TYPES, TransactionStatus, TransactionType
from ..core.money import DEFAULT_CURRENCY
from ..db.base import utc_now
from ..db.models import (
    Account,
    AppSetting,
    AssetSnapshot,
    DailyStat,
    DayEvent,
    Transaction,
    TransactionSplit,
)

__all__ = [
    "CALENDAR_METRICS",
    "DAY_EVENT_KINDS",
    "ENTRY_STATES",
    "CalendarCell",
    "confirm_day",
    "create_event",
    "delete_event",
    "ensure_fresh",
    "get_calendar",
    "get_day_detail",
    "invalidate_all",
    "mark_dirty_from",
    "net_worth_series",
    "recompute_range",
]

_logger = logging.getLogger(__name__)

#: 脏标记的"全脏"起点。比任何真实流水都早。
_EPOCH = date(1970, 1, 1)
_STALE_COMPUTED_AT = datetime(1970, 1, 1)

#: 异常分数回看的周数（同星期基线）
_ANOMALY_LOOKBACK_WEEKS = 8

#: 脏标记在 ``app_settings`` 中的键
_DIRTY_KEY = "daily_stats_dirty_from"
_FORMULA_KEY = "daily_stats_formula_version"
_FORMULA_VERSION = 2

#: 日历颜色主指标（REQ-20 的六类）
CALENDAR_METRICS: tuple[str, ...] = (
    "entry",  # 是否登记收支（默认）
    "expense",  # 支出强度
    "income",  # 收入强度
    "net",  # 净流入 / 净流出（发散色阶）
    "net_worth_change",  # 资产变化幅度（相对当日净值的百分比）
    "anomaly",  # 异常分数
)

#: 登记状态
ENTRY_STATES: tuple[str, ...] = ("none", "logged", "confirmed")

#: 事件日志类型
DAY_EVENT_KINDS: tuple[str, ...] = ("event", "mood", "anniversary", "note", "todo")

CalendarMetric = Literal["entry", "expense", "income", "net", "net_worth_change", "anomaly"]


# -----------------------------------------------------------------------------
# 标记与重算
# -----------------------------------------------------------------------------
def _read_dirty(session: Session) -> date | None:
    setting = session.get(AppSetting, _DIRTY_KEY)
    if setting is None:
        return None
    raw = setting.value.get("from")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:  # pragma: no cover - 数据损坏时按"全脏"处理更安全
        return _EPOCH


def _write_dirty(session: Session, value: date | None) -> None:
    setting = session.get(AppSetting, _DIRTY_KEY)
    payload = {"from": value.isoformat()} if value else {}
    if setting is None:
        session.add(AppSetting(key=_DIRTY_KEY, value=payload))
    else:
        setting.value = payload
    session.flush()


def mark_dirty_from(session: Session, day: date | datetime | None = None) -> None:
    """把从 ``day`` 起的日结标记为需要重算。

    **保守取值**：调用方不确定影响范围时传 ``None``（等价于"全部重算"）。
    多算一次的代价是几十毫秒，算错的代价是用户看到错的余额。
    """
    if day is None:
        candidate = _EPOCH
    elif isinstance(day, datetime):
        candidate = day.date()
    else:
        candidate = day

    current = _read_dirty(session)
    if current is None or candidate < current:
        _write_dirty(session, candidate)


def invalidate_all(session: Session) -> None:
    """把全部日结标记为脏（账户结构变化、手工校准余额时使用）。"""
    _write_dirty(session, _EPOCH)


def recompute_range(session: Session, start: date, end: date) -> int:
    """重算 ``[start, end]`` 的每日汇总与账户快照，返回重算的天数。

    净值是**累计量**：某天插入一笔流水会改变当天及之后所有天的余额。
    因此必须先把区间起点之前的余额算出来作为起点，再逐日推进。
    """
    if end < start:
        return 0

    accounts = _active_accounts(session)
    opening = _balances_before(session, accounts, start)
    # 流水按日汇总时**多取 8 周**：异常分数需要"当天之前 8 周的同星期基线"。
    #
    # 这里曾写错一次：基线以"重算区间的起点"为基准回溯，于是区间内部的日期
    # 会拿到区间之外的基线，异常分数整体失真（而表面上数字是有的、也不报错）。
    # 正确做法是让每个日期各自向前看 8 周，因此把查询窗口一起扩出去。
    lookback = start - timedelta(weeks=_ANOMALY_LOOKBACK_WEEKS)
    flows = _daily_flows(session, lookback, end)
    event_counts = _event_counts(session, start, end)
    top_categories = _top_categories(session, start, end)

    days = _iter_days(start, end)
    # 附件日集合在循环外算一次：放进循环里会变成 N 次查询
    attachment_days = _attachment_days(session, start, end)

    balances = dict(opening)
    counted = [
        account
        for account in accounts
        if account.include_in_net_worth and account.currency == DEFAULT_CURRENCY
    ]
    net_worth = sum(opening.get(account.id, 0) for account in counted)

    recomputed = 0
    for day in days:
        bucket = flows.get(day, {})
        for account_id, delta in bucket.get("per_account", {}).items():
            balances[account_id] = balances.get(account_id, 0) + delta
        net_worth = sum(balances.get(account.id, 0) for account in counted)

        _upsert_snapshots(session, day, accounts, balances)

        event_count = event_counts.get(day, 0)
        tx_count = bucket.get("tx_count", 0)
        existing = session.get(DailyStat, day)
        confirmed_at = existing.entry_confirmed_at if existing is not None else None
        if confirmed_at is not None:
            entry_state = "confirmed"
        elif tx_count == 0 and event_count == 0:
            entry_state = "none"
        else:
            entry_state = "logged"

        _upsert_daily_stat(
            session,
            day,
            income_minor=bucket.get("income", 0),
            expense_minor=bucket.get("expense", 0),
            net_minor=bucket.get("income", 0) - bucket.get("expense", 0),
            net_worth_minor=net_worth,
            transfer_in_minor=bucket.get("transfer_in", 0),
            transfer_out_minor=bucket.get("transfer_out", 0),
            tx_count=tx_count,
            pending_count=bucket.get("pending", 0),
            entry_state=entry_state,
            entry_confirmed_at=confirmed_at,
            top_category_id=top_categories.get(day),
            anomaly_score=_anomaly_score(day, flows),
            event_count=event_count,
            has_attachment=day in attachment_days,
        )
        recomputed += 1

    session.flush()
    # K 线的 OHLC 是 daily_stats 的聚合，因此日结一变它就必须作废。
    # 这里只作废、不重算：一次改动触发全部周期的重算会明显变慢，
    # 而实际只会读到用户正在看的那个周期。
    #
    # 局部导入是为了打断 daily <-> kline 的循环依赖（kline 需要 daily 保证
    # 日结新鲜）。这是有意的取舍：两者本来就是互相依赖的关系，
    # 用一个函数内的导入表达它，比把其中一方拆成第三个模块更直白。
    from . import kline as kline_service

    kline_service.invalidate(session, start, end)
    return recomputed


def ensure_fresh(session: Session, start: date, end: date) -> None:
    """Mark affected caches stale, then recompute only the requested days.

    Opening balances come from transactions, so intermediate days need not be materialized.
    Stale rows outside this window remain marked until their next read.
    """
    if end < start:
        return
    formula = session.get(AppSetting, _FORMULA_KEY)
    if formula is None or formula.value.get("version") != _FORMULA_VERSION:
        if formula is None:
            session.add(AppSetting(key=_FORMULA_KEY, value={"version": _FORMULA_VERSION}))
        else:
            formula.value = {"version": _FORMULA_VERSION}
        invalidate_all(session)
    dirty = _read_dirty(session)
    if dirty is not None:
        session.execute(
            update(DailyStat).where(DailyStat.date >= dirty).values(computed_at=_STALE_COMPUTED_AT)
        )
        from . import kline as kline_service

        kline_service.invalidate(session, dirty, date.max)
        _write_dirty(session, None)
    have = (
        session.scalar(
            select(func.count(DailyStat.date)).where(
                DailyStat.date.between(start, end), DailyStat.computed_at > _STALE_COMPUTED_AT
            )
        )
        or 0
    )
    if have == (end - start).days + 1:
        return
    days = recompute_range(session, start, end)
    if days:
        _logger.debug("日结重算：%s → %s（%s 天）", start, end, days)


# -----------------------------------------------------------------------------
# 读取：日历
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class CalendarCell:
    """日历一格。

    ``level`` 是**服务端算好的强度分级**，而不是让前端自己定阈值：
    图例、日历、导出 PNG 必须用同一套分级，否则"看起来一样深的两格"含义不同。
    """

    date: date
    income_minor: int = 0
    expense_minor: int = 0
    net_minor: int = 0
    net_worth_minor: int = 0
    tx_count: int = 0
    pending_count: int = 0
    entry_state: str = "none"
    anomaly_score: float = 0.0
    top_category_id: int | None = None
    event_count: int = 0
    has_attachment: bool = False
    #: 主指标原始值（前端悬浮显示用；``net_worth_change`` 为比例而非金额）
    metric_value: float = 0.0
    #: 分级：``entry`` 为 0..2，其余为 0..4
    level: int = 0
    #: 角标：``bill`` 账单日 / ``due`` 还款日 / ``events`` 有事件 /
    #: ``attachment`` 有附件 / ``transfer`` 有转账
    badges: list[str] = field(default_factory=list)


def get_calendar(
    session: Session,
    *,
    start: date,
    end: date,
    metric: CalendarMetric = "entry",
) -> list[CalendarCell]:
    """返回区间内**每一天**的日历格（没有记录的日期也会出现）。

    占位是刻意的：日历缺格会让用户以为应用坏了，
    而"这天没记账"本身就是要展示的信息（REQ-20 的默认指标）。
    """
    if metric not in CALENDAR_METRICS:
        metric = "entry"
    ensure_fresh(session, start, end)

    rows = {
        row.date: row
        for row in session.scalars(select(DailyStat).where(DailyStat.date.between(start, end))).all()
    }
    badges = _badge_map(session, start, end)

    # 净值变化率需要"前一天"的净值：多取一天即可，不需要额外存列
    previous = _net_worth_at(session, start - timedelta(days=1))

    cells: list[CalendarCell] = []
    for day in _iter_days(start, end):
        row = rows.get(day)
        cell = CalendarCell(
            date=day,
            income_minor=row.income_minor if row else 0,
            expense_minor=row.expense_minor if row else 0,
            net_minor=row.net_minor if row else 0,
            net_worth_minor=row.net_worth_minor if row else 0,
            tx_count=row.tx_count if row else 0,
            pending_count=row.pending_count if row else 0,
            entry_state=row.entry_state if row else "none",
            anomaly_score=row.anomaly_score if row else 0.0,
            top_category_id=row.top_category_id if row else None,
            event_count=row.event_count if row else 0,
            has_attachment=bool(row.has_attachment) if row else False,
        )
        if metric == "net_worth_change":
            cell.metric_value = _change_ratio(previous, cell.net_worth_minor)
        else:
            cell.metric_value = _metric_value(cell, metric)
        cell.badges = badges.get(day, [])
        cells.append(cell)
        previous = cell.net_worth_minor

    _assign_levels(cells, metric)
    return cells


def _change_ratio(previous: int, current: int) -> float:
    """净值相对前一日的变化比例。

    用比例而不是绝对金额：净值 10 万变动 1000 与净值 1 万变动 1000
    在"今天资产波动大不大"这个问题上的答案完全不同。
    前一日净值为 0 时返回 0（比例无定义，不编造）。
    """
    if previous == 0:
        return 0.0
    return (current - previous) / abs(previous)


def _metric_value(cell: CalendarCell, metric: CalendarMetric) -> float:
    if metric == "entry":
        # 三级：未登记 / 已登记 / 已核对
        return {"none": 0.0, "logged": 1.0, "confirmed": 2.0}[cell.entry_state]
    if metric == "expense":
        return float(cell.expense_minor)
    if metric == "income":
        return float(cell.income_minor)
    if metric == "net":
        return float(cell.net_minor)
    if metric == "anomaly":
        return float(cell.anomaly_score)
    return 0.0


def _assign_levels(cells: list[CalendarCell], metric: CalendarMetric) -> None:
    """按分位数给每一天定强度分级。

    为什么用**分位数**而不是固定阈值：不同人的支出量级相差几个数量级，
    固定阈值会让"月支出 3000 的人"整月都是最浅色、"月支出 30 万的人"整月最深色。
    分位数保证日历总能把色域用满（GitHub 贡献图也是这个思路）。
    """
    if metric == "entry":
        levels = {"none": 0, "logged": 1, "confirmed": 2}
        for cell in cells:
            cell.level = levels[cell.entry_state]
        return

    if metric == "net":
        # 发散色阶：正负各两级，0 为中性。1 深绿 / 2 浅绿 / 3 浅红 / 4 深红
        magnitudes = [abs(cell.metric_value) for cell in cells if cell.metric_value != 0]
        threshold = median(magnitudes) if magnitudes else 0.0
        for cell in cells:
            value = cell.metric_value
            if value == 0:
                cell.level = 0
            elif value > 0:
                cell.level = 2 if threshold == 0 or value <= threshold else 1
            else:
                cell.level = 3 if threshold == 0 or -value <= threshold else 4
        return

    values = sorted(cell.metric_value for cell in cells if cell.metric_value > 0)
    if not values:
        for cell in cells:
            cell.level = 0
        return

    def quantile(position: float) -> float:
        index = min(len(values) - 1, max(0, round(position * (len(values) - 1))))
        return values[index]

    # 三个四分位切点 → 正值的四档（1..4），加上"零"的 0 档，
    # 正好是 GitHub 贡献图的五级色阶。用四个切点会有一档永远空着
    # （曾写成那样，结果是最高一档跨了 40% 的分布，颜色看起来"跳"）。
    cuts = [quantile(0.25), quantile(0.5), quantile(0.75)]
    for cell in cells:
        value = cell.metric_value
        if value <= 0:
            cell.level = 0
        elif value <= cuts[0]:
            cell.level = 1
        elif value <= cuts[1]:
            cell.level = 2
        elif value <= cuts[2]:
            cell.level = 3
        else:
            cell.level = 4


def _badge_map(session: Session, start: date, end: date) -> dict[date, list[str]]:
    """角标：事件 / 附件 / 账单日 / 还款日 / 转账。

    发薪日与预算角标分别在 P6 与预算模块落地 —— 现在**不显示一个永远不亮的角标**，
    那比没有角标更让人困惑。
    """
    badges: dict[date, list[str]] = defaultdict(list)

    for day in session.scalars(
        select(DayEvent.date).where(DayEvent.deleted_at.is_(None), DayEvent.date.between(start, end))
    ).all():
        badges[day].append("events")

    for day in _attachment_days(session, start, end):
        badges[day].append("attachment")

    transfer_days = session.scalars(
        select(func.date(Transaction.occurred_at))
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.type.in_([item.value for item in TRANSFER_TYPES]),
            func.date(Transaction.occurred_at).between(start.isoformat(), end.isoformat()),
        )
        .distinct()
    ).all()
    for raw in transfer_days:
        day = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        badges[day].append("transfer")

    # 账单日 / 还款日来自账户配置（信用卡）。同一天既是账单日又是还款日时
    # 只显示"还款日" —— 那件事更需要行动
    accounts = session.scalars(
        select(Account).where(Account.deleted_at.is_(None), Account.is_archived.is_(False))
    ).all()
    for day in _iter_days(start, end):
        has_bill = False
        for account in accounts:
            if account.due_day and account.due_day == day.day:
                badges[day].append("due")
                has_bill = False
                break
            if account.bill_day and account.bill_day == day.day:
                has_bill = True
        if has_bill:
            badges[day].append("bill")
    return badges


# -----------------------------------------------------------------------------
# 读取：当日详情
# -----------------------------------------------------------------------------
def get_day_detail(session: Session, day: date) -> dict[str, Any]:
    """当日详情：余额阶梯曲线、收支构成、流水清单、事件、变动归因。

    这是 REQ-20 的完整闭环 —— 点开某一天应该能回答
    "这天发生了什么、钱怎么变的"，而不只是列几条流水。
    """
    ensure_fresh(session, day, day)
    stat = session.get(DailyStat, day)

    starts, ends = _day_bounds(day)
    transactions = list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.deleted_at.is_(None),
                Transaction.occurred_at >= starts,
                Transaction.occurred_at <= ends,
            )
            .order_by(Transaction.occurred_at, Transaction.id)
        ).all()
    )
    events = list(
        session.scalars(
            select(DayEvent)
            .where(DayEvent.deleted_at.is_(None), DayEvent.date == day)
            .order_by(DayEvent.sort_order, DayEvent.id)
        ).all()
    )

    opening = _net_worth_at(session, day - timedelta(days=1))

    # 余额阶梯曲线：从"昨日收盘"开始，按流水**真实时点**逐笔推进。
    # 不做按小时均匀采样 —— 曲线的拐点必须落在真实发生的那一刻，
    # 否则它会看起来像数据，实际是插值出来的幻觉。
    steps: list[dict[str, Any]] = [
        {"at": f"{day.isoformat()}T00:00:00", "net_worth_minor": opening, "label": "opening"}
    ]
    running = opening
    contributions: list[dict[str, Any]] = []
    for transaction in transactions:
        delta = _net_worth_delta(transaction)
        if delta == 0:
            continue
        running += delta
        label = transaction.payee or transaction.note or "—"
        steps.append(
            {
                "at": transaction.occurred_at.isoformat(),
                "net_worth_minor": running,
                "label": label,
                "transaction_id": transaction.id,
            }
        )
        contributions.append(
            {
                "transaction_id": transaction.id,
                "label": label,
                "type": transaction.type,
                "delta_minor": delta,
                "category_name": transaction.category.name if transaction.category else "",
            }
        )
    contributions.sort(key=lambda item: abs(item["delta_minor"]), reverse=True)

    composition = _day_composition(transactions)
    names = _category_names(session, composition)

    return {
        "date": day.isoformat(),
        "stat": {
            "income_minor": stat.income_minor if stat else 0,
            "expense_minor": stat.expense_minor if stat else 0,
            "net_minor": stat.net_minor if stat else 0,
            "net_worth_minor": stat.net_worth_minor if stat else opening,
            "opening_net_worth_minor": opening,
            "tx_count": stat.tx_count if stat else 0,
            "entry_state": stat.entry_state if stat else "none",
            "anomaly_score": stat.anomaly_score if stat else 0.0,
            "event_count": stat.event_count if stat else 0,
        },
        "net_worth_series": steps,
        "composition": [
            {
                "category_id": category_id,
                "category_name": names.get(category_id, "未分类") if category_id else "未分类",
                "amount_minor": amount,
            }
            for category_id, amount in composition
        ],
        "weekday_average_expense_minor": _same_weekday_average(session, day),
        "contributions": contributions,
        "events": [
            {
                "id": event.id,
                "kind": event.kind,
                "title": event.title,
                "body": event.body,
                "tags": event.tags,
                "attachments": event.attachments,
            }
            for event in events
        ],
        "transactions": transactions,
    }


def confirm_day(session: Session, day: date, *, confirmed: bool = True) -> None:
    """把某天标记为「已核对」。

    这是 ``entry_state`` 里 ``confirmed`` 的**唯一来源**。
    刻意做成用户动作而不是自动判定：应用无法知道"这天是不是还有没记的账"，
    假装知道只会产生一个不可信的指标。
    """
    ensure_fresh(session, day, day)
    stat = session.get(DailyStat, day)
    if stat is None:  # pragma: no cover - ensure_fresh 后必然存在
        return
    stat.entry_confirmed_at = utc_now() if confirmed else None
    if confirmed:
        stat.entry_state = "confirmed"
    elif stat.tx_count > 0 or stat.event_count > 0:
        stat.entry_state = "logged"
    else:
        stat.entry_state = "none"
    session.flush()


# -----------------------------------------------------------------------------
# 事件日志
# -----------------------------------------------------------------------------
def create_event(
    session: Session,
    *,
    day: date,
    kind: str = "event",
    title: str = "",
    body: str = "",
    tags: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> DayEvent:
    """新增一条当日事件，并让这一天的日结失效。"""
    if kind not in DAY_EVENT_KINDS:
        kind = "event"
    event = DayEvent(
        date=day,
        kind=kind,
        title=title.strip()[:96],
        body=body,
        tags=list(tags or []),
        attachments=list(attachments or []),
    )
    session.add(event)
    session.flush()
    mark_dirty_from(session, day)
    return event


def delete_event(session: Session, event_id: int) -> None:
    event = session.get(DayEvent, event_id)
    if event is None or event.deleted_at is not None:
        return
    event.soft_delete()
    session.flush()
    mark_dirty_from(session, event.date)


# -----------------------------------------------------------------------------
# 读取：净值序列
# -----------------------------------------------------------------------------
def net_worth_series(session: Session, *, start: date, end: date) -> list[dict[str, Any]]:
    """按日返回净值（时序图的唯一数据源）。"""
    ensure_fresh(session, start, end)
    rows = session.execute(
        select(DailyStat.date, DailyStat.net_worth_minor, DailyStat.income_minor, DailyStat.expense_minor)
        .where(DailyStat.date.between(start, end))
        .order_by(DailyStat.date)
    ).all()
    by_day = {
        row.date: (int(row.net_worth_minor or 0), int(row.income_minor or 0), int(row.expense_minor or 0))
        for row in rows
    }
    series: list[dict[str, Any]] = []
    for day in _iter_days(start, end):
        net_worth, income, expense = by_day.get(day, (0, 0, 0))
        series.append(
            {
                "date": day.isoformat(),
                "net_worth_minor": net_worth,
                "income_minor": income,
                "expense_minor": expense,
                "net_minor": income - expense,
            }
        )
    return series


# -----------------------------------------------------------------------------
# 内部：聚合
# -----------------------------------------------------------------------------
def _iter_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    return datetime.combine(day, time.min), datetime.combine(day, time.max)


def _active_accounts(session: Session) -> list[Account]:
    """参与快照的账户。

    包含已归档账户：历史快照要能反映"当时它还在用"。
    不含已软删除的账户 —— 删除账户时已被拒绝（见 ``services/accounts.py``），
    因此不存悬空引用。
    """
    return list(
        session.scalars(
            select(Account).where(Account.deleted_at.is_(None)).order_by(Account.sort_order, Account.id)
        ).all()
    )


def _balances_before(session: Session, accounts: list[Account], start: date) -> dict[int, int]:
    """``start`` 之前所有流水的净影响 + 各账户起点余额。"""
    balances = {account.id: account.initial_balance_minor for account in accounts}
    if not accounts:
        return balances
    cutoff = datetime.combine(start, time.min)
    live = (
        Transaction.deleted_at.is_(None),
        Transaction.status != TransactionStatus.VOID.value,
        Transaction.occurred_at < cutoff,
    )

    outgoing = session.execute(
        select(
            Transaction.account_id,
            func.sum(
                case(
                    (Transaction.direction == "in", Transaction.amount_minor),
                    else_=-Transaction.amount_minor,
                )
            ),
        )
        .where(*live)
        .group_by(Transaction.account_id)
    ).all()
    for account_id, net in outgoing:
        if account_id in balances:
            balances[account_id] += int(net or 0)

    incoming = session.execute(
        select(Transaction.to_account_id, func.sum(Transaction.amount_minor))
        .where(*live, Transaction.to_account_id.is_not(None))
        .group_by(Transaction.to_account_id)
    ).all()
    for account_id, net in incoming:
        if account_id in balances:
            balances[account_id] += int(net or 0)
    return balances


def _daily_flows(session: Session, start: date, end: date) -> dict[date, dict[str, Any]]:
    """区间内每天的收支、笔数与**每账户净变动**（快照用）。"""
    starts, ends = datetime.combine(start, time.min), datetime.combine(end, time.max)
    rows = session.execute(
        select(
            func.date(Transaction.occurred_at).label("day"),
            Transaction.account_id,
            Transaction.type,
            Transaction.direction,
            Transaction.currency,
            func.sum(Transaction.amount_minor).label("total"),
            func.count(Transaction.id).label("count"),
        )
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.status != TransactionStatus.VOID.value,
            Transaction.occurred_at >= starts,
            Transaction.occurred_at <= ends,
        )
        .group_by(
            func.date(Transaction.occurred_at),
            Transaction.account_id,
            Transaction.type,
            Transaction.direction,
            Transaction.currency,
        )
    ).all()

    transfer_types = {item.value for item in TRANSFER_TYPES}
    buckets: dict[date, dict[str, Any]] = {}

    def bucket_for(day: date) -> dict[str, Any]:
        return buckets.setdefault(
            day,
            {
                "income": 0,
                "expense": 0,
                "transfer_in": 0,
                "transfer_out": 0,
                "tx_count": 0,
                "pending": 0,
                "per_account": defaultdict(int),
            },
        )

    for row in rows:
        day = row.day if isinstance(row.day, date) else date.fromisoformat(str(row.day))
        bucket = bucket_for(day)
        amount = int(row.total or 0)
        bucket["tx_count"] += int(row.count or 0)

        if row.currency == DEFAULT_CURRENCY and row.type == TransactionType.INCOME.value:
            bucket["income"] += amount
        elif row.currency == DEFAULT_CURRENCY and row.type == TransactionType.EXPENSE.value:
            bucket["expense"] += amount
        if row.currency == DEFAULT_CURRENCY and row.type in transfer_types:
            if row.direction == "in":
                bucket["transfer_in"] += amount
            else:
                bucket["transfer_out"] += amount

        # 账户层面净变动：``in`` 为正、``out`` 为负（与余额公式同一套语义）
        bucket["per_account"][row.account_id] += amount if row.direction == "in" else -amount

    # 转入方单独叠加：上面按 ``account_id`` 分组时看不到 ``to_account_id``
    incoming = session.execute(
        select(
            func.date(Transaction.occurred_at).label("day"),
            Transaction.to_account_id,
            func.sum(Transaction.amount_minor).label("total"),
        )
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.status != TransactionStatus.VOID.value,
            Transaction.occurred_at >= starts,
            Transaction.occurred_at <= ends,
            Transaction.to_account_id.is_not(None),
        )
        .group_by(func.date(Transaction.occurred_at), Transaction.to_account_id)
    ).all()
    for row in incoming:
        day = row.day if isinstance(row.day, date) else date.fromisoformat(str(row.day))
        bucket_for(day)["per_account"][row.to_account_id] += int(row.total or 0)

    pending = session.execute(
        select(func.date(Transaction.occurred_at), func.count(Transaction.id))
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.status == TransactionStatus.PENDING.value,
            Transaction.occurred_at >= starts,
            Transaction.occurred_at <= ends,
        )
        .group_by(func.date(Transaction.occurred_at))
    ).all()
    for raw_day, count in pending:
        day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        bucket_for(day)["pending"] += int(count or 0)

    return buckets


def _event_counts(session: Session, start: date, end: date) -> dict[date, int]:
    rows = session.execute(
        select(DayEvent.date, func.count(DayEvent.id))
        .where(DayEvent.deleted_at.is_(None), DayEvent.date.between(start, end))
        .group_by(DayEvent.date)
    ).all()
    return {row[0]: int(row[1] or 0) for row in rows}


def _attachment_days(session: Session, start: date, end: date) -> set[date]:
    """有附件的日子。

    **只统计事件日志的附件**：流水附件要到 P8 才落地，
    现在假装能算出来只会得到一个永远为 False 的角标（那比没有更让人困惑）。
    事件数量在个位数量级，因此直接在 Python 侧判断，不做 JSON 函数查询 ——
    少依赖一个 SQLite 扩展，也就少一个跨环境的坑。
    """
    days: set[date] = set()
    for event in session.scalars(
        select(DayEvent).where(DayEvent.deleted_at.is_(None), DayEvent.date.between(start, end))
    ).all():
        if event.attachments:
            days.add(event.date)
    return days


def _top_categories(session: Session, start: date, end: date) -> dict[date, int | None]:
    """每天支出最多的分类（分账优先）。"""
    starts, ends = datetime.combine(start, time.min), datetime.combine(end, time.max)
    common = (
        Transaction.currency == DEFAULT_CURRENCY,
        Transaction.deleted_at.is_(None),
        Transaction.status != TransactionStatus.VOID.value,
        Transaction.type == TransactionType.EXPENSE.value,
        Transaction.occurred_at >= starts,
        Transaction.occurred_at <= ends,
    )

    split_rows = session.execute(
        select(
            func.date(Transaction.occurred_at).label("day"),
            TransactionSplit.category_id,
            func.sum(TransactionSplit.amount_minor).label("total"),
        )
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(*common, Transaction.splits.any())
        .group_by(func.date(Transaction.occurred_at), TransactionSplit.category_id)
    ).all()

    plain_rows = session.execute(
        select(
            func.date(Transaction.occurred_at).label("day"),
            Transaction.category_id,
            func.sum(Transaction.amount_minor).label("total"),
        )
        .where(*common, ~Transaction.splits.any())
        .group_by(func.date(Transaction.occurred_at), Transaction.category_id)
    ).all()

    totals: dict[date, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
    for row in [*split_rows, *plain_rows]:
        day = row.day if isinstance(row.day, date) else date.fromisoformat(str(row.day))
        totals[day][row.category_id] += int(row.total or 0)

    return {day: max(by_category.items(), key=lambda item: item[1])[0] for day, by_category in totals.items()}


def _anomaly_score(day: date, flows: dict[date, dict[str, Any]]) -> float:
    """异常分数：与"同星期近 8 周基线中位数"的偏离，归一化到 0..1。

    基线**逐日回溯**（每个日期各自向前看 8 周），而不是全区间共用一条基线 ——
    后者会让区间深处的日期拿到错误的参照。

    口径（必须写清楚，否则这个数字会被误读）：
        * 只统计**有记录的日子**。没记账的那天是"未知"，不是"花了 0"；
          把它当作 0 会把基线拉到 0，于是所有正常消费都成了"异常"；
        * 基线取**中位数**而不是均值 —— 均值会被一次大额消费拉高，
          导致之后的正常消费全被判成"异常"；
        * 没有历史样本且有支出 → 1.0（第一次在某星期消费本身就值得注意）；
        * 偏离 3 倍及以上 → 1.0（封顶）。
    """
    value = int(flows.get(day, {}).get("expense", 0))
    samples: list[int] = []
    for weeks in range(1, _ANOMALY_LOOKBACK_WEEKS + 1):
        candidate = day - timedelta(weeks=weeks)
        bucket = flows.get(candidate)
        if bucket is None or not bucket.get("tx_count"):
            continue
        samples.append(int(bucket.get("expense", 0)))

    if not samples:
        return 1.0 if value > 0 else 0.0
    baseline = median(samples)
    if baseline <= 0:
        return 1.0 if value > 0 else 0.0
    ratio = value / baseline
    if ratio <= 1:
        return 0.0
    return min(1.0, (ratio - 1) / 2)


def _net_worth_at(session: Session, day: date) -> int:
    row = session.get(DailyStat, day)
    dirty = _read_dirty(session)
    if (
        row is not None
        and row.computed_at.replace(tzinfo=None) > _STALE_COMPUTED_AT
        and (dirty is None or dirty > day)
    ):
        return row.net_worth_minor
    accounts = _active_accounts(session)
    balances = _balances_before(session, accounts, day + timedelta(days=1))
    return sum(
        balances.get(account.id, 0)
        for account in accounts
        if account.include_in_net_worth and account.currency == DEFAULT_CURRENCY
    )


def _net_worth_delta(transaction: Transaction) -> int:
    """单笔流水对**总净值**的影响。

    转账与校准不改变总净值（钱只是换个口袋），因此贡献为 0 ——
    否则日内曲线会因为一次账户间划转而出现虚假的尖峰。
    """
    if transaction.currency != DEFAULT_CURRENCY or transaction.type in {
        item.value for item in TRANSFER_TYPES
    }:
        return 0
    return transaction.amount_minor if transaction.direction == "in" else -transaction.amount_minor


def _day_composition(transactions: list[Transaction]) -> list[tuple[int | None, int]]:
    totals: dict[int | None, int] = defaultdict(int)
    for transaction in transactions:
        if transaction.currency != DEFAULT_CURRENCY or transaction.type != TransactionType.EXPENSE.value:
            continue
        if transaction.splits:
            for split in transaction.splits:
                totals[split.category_id] += split.amount_minor
        else:
            totals[transaction.category_id] += transaction.amount_minor
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)


def _same_weekday_average(session: Session, day: date) -> int:
    """近 30 天同星期的平均支出（"今天花得比平时多吗"的对比基线）。"""
    rows = session.execute(
        select(func.date(Transaction.occurred_at), func.sum(Transaction.amount_minor))
        .where(
            Transaction.currency == DEFAULT_CURRENCY,
            Transaction.deleted_at.is_(None),
            Transaction.status != TransactionStatus.VOID.value,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.occurred_at >= datetime.combine(day - timedelta(days=30), time.min),
            Transaction.occurred_at < datetime.combine(day, time.min),
        )
        .group_by(func.date(Transaction.occurred_at))
    ).all()
    samples = [
        int(row[1] or 0)
        for row in rows
        if (row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))).weekday()
        == day.weekday()
    ]
    return int(sum(samples) / len(samples)) if samples else 0


def _category_names(session: Session, composition: list[tuple[int | None, int]]) -> dict[int, str]:
    from ..db.models import Category

    ids = [item[0] for item in composition if item[0]]
    if not ids:
        return {}
    rows = session.execute(select(Category.id, Category.name).where(Category.id.in_(ids))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _upsert_snapshots(session: Session, day: date, accounts: list[Account], balances: dict[int, int]) -> None:
    for account in accounts:
        balance = balances.get(account.id, 0)
        net_worth = balance if account.include_in_net_worth else 0
        existing = session.scalar(
            select(AssetSnapshot).where(
                AssetSnapshot.snapshot_date == day, AssetSnapshot.account_id == account.id
            )
        )
        if existing is None:
            session.add(
                AssetSnapshot(
                    snapshot_date=day,
                    account_id=account.id,
                    balance_minor=balance,
                    net_worth_minor=net_worth,
                    source="auto",
                )
            )
        elif existing.source != "manual":
            # 手动校准过的快照不被自动日结覆盖 —— 那是用户明确表达过的口径
            existing.balance_minor = balance
            existing.net_worth_minor = net_worth


def _upsert_daily_stat(session: Session, day: date, **values: Any) -> None:
    row = session.get(DailyStat, day)
    if row is None:
        session.add(DailyStat(date=day, computed_at=utc_now(), **values))
        return
    for key, value in values.items():
        setattr(row, key, value)
    row.computed_at = utc_now()
