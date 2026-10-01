"""工作日与发薪日（P6 / 需求 19）。

为什么不内置法定节假日
======================
中国的法定节假日与调休每年由国务院办公厅另行发布，各省市还可能有地方性假期。
写死在代码里的日期表**必然过期，而且过期时是静默的**；
更要紧的是**发错日期比不发更糟** —— 用户会按"系统说 28 日发薪"去安排资金。

所以这里：

* **周末由代码算**（与年份无关，永远正确）；
* **法定节假日与调休由用户录入**（`workday_calendar` 逐日覆盖），
  并提供按年**批量粘贴**的解析器 —— 官方发布的就是一份很短的列表；
* **`is_workday` 会如实报告置信度**：这一年没有任何节假日数据时，
  一个周一到周五的日子只能算"**推测**是工作日"，而不是确定。

一句话：**未知就是未知，不能伪装成工作日。**
调用方（发薪日计算、周期记账）把这份不确定性一路带到界面上。
"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..db.models import WorkdayCalendar

__all__ = [
    "DAY_KINDS",
    "POLICIES",
    "PaydayResolution",
    "WorkdayFact",
    "coverage_years",
    "import_workday_text",
    "is_holiday",
    "is_weekend",
    "is_workday",
    "list_overrides",
    "month_workdays",
    "nth_workday_of_month",
    "parse_workday_text",
    "resolve_payday",
    "shift_to_workday",
    "upcoming_paydays",
    "upsert_override",
    "workday_fact",
]

_logger = logging.getLogger(__name__)

#: 发薪日的四种写法
DAY_KINDS = ("fixed", "month_end", "last_workday", "nth_workday")

#: 遇到非工作日时的调整策略
POLICIES = ("advance", "postpone", "none")

#: 一次最多回溯/前推多少天。设上界是为了让"整年都是假期"这类坏数据
#: 得到一个明确的错误，而不是一个死循环
_MAX_SHIFT_DAYS = 60


# -----------------------------------------------------------------------------
# 单日判定
# -----------------------------------------------------------------------------
def is_weekend(day: date) -> bool:
    """周六周日。**与年份无关，因此永远可信。**"""
    return day.weekday() >= 5


def _override_map(session: Session, days: Iterable[date]) -> dict[date, WorkdayCalendar]:
    """一次取出这批日期的覆盖记录，避免逐日查询（N+1）。

    发薪日计算要回溯几十天，逐日查库会变成几十次往返。
    """
    wanted = list({day for day in days})
    if not wanted:
        return {}
    rows = session.scalars(select(WorkdayCalendar).where(WorkdayCalendar.day.in_(wanted))).all()
    return {row.day: row for row in rows}


def is_holiday(session: Session, day: date) -> str:
    """这一天是不是法定节假日？返回假期名称，不是则返回空串。"""
    row = session.scalar(select(WorkdayCalendar).where(WorkdayCalendar.day == day))
    if row is None:
        return ""
    return row.name if not row.is_workday else ""


def is_workday(session: Session, day: date) -> bool:
    """这一天是不是工作日。

    **不确定时按"是"返回**（见 `WorkdayFact.confident`）——
    把未知当成"不上班"会让发薪日被无端推迟，而推迟比不动更麻烦。
    但调用方必须能看到那份不确定性，因此用 `workday_fact()`。
    """
    return workday_fact(session, day).is_workday


@dataclass(slots=True)
class WorkdayFact:
    """一天的工作日判定结果，**带上它有多可信**。"""

    day: date
    is_workday: bool
    #: 判定依据：'override'（有明确记录）/ 'weekend'（周末且该年有数据）/
    #: 'assumed'（该年没有节假日数据，只能按周一至周五推测）
    basis: str
    #: 有明确记录、或该年已有节假日数据时为 True
    confident: bool
    name: str = ""
    kind: str = ""

    @property
    def assumed(self) -> bool:
        return not self.confident


def workday_fact(session: Session, day: date) -> WorkdayFact:
    """完整判定：结果 + 依据 + 是否可信。"""
    row = session.scalar(select(WorkdayCalendar).where(WorkdayCalendar.day == day))
    if row is not None:
        return WorkdayFact(
            day=day,
            is_workday=row.is_workday,
            basis="override",
            confident=True,
            name=row.name,
            kind=row.kind,
        )
    weekend = is_weekend(day)
    covered = coverage_years(session)
    # 周末在**任何**年份都不可信地"不上班"（调休只可能把周末变成工作日，
    # 而那种情况一定会有 override），因此周末可以确定。
    if weekend:
        return WorkdayFact(day=day, is_workday=False, basis="weekend", confident=True)
    if day.year in covered:
        return WorkdayFact(day=day, is_workday=True, basis="weekday", confident=True)
    # 该年没有任何节假日数据：一个周中日子很可能就是工作日，
    # 但它也可能是春节假期 —— 这份不确定性必须传出去
    return WorkdayFact(day=day, is_workday=True, basis="assumed", confident=False)


def coverage_years(session: Session) -> set[int]:
    """哪些年份已经录入过节假日/调休数据。

    判据是"这一年有没有任何一条记录"，而不是"有没有 365 条" ——
    因为本表只存例外（见模型 docstring）。
    """
    rows = session.execute(
        select(func.strftime("%Y", WorkdayCalendar.day)).group_by(func.strftime("%Y", WorkdayCalendar.day))
    ).all()
    return {int(row[0]) for row in rows if row[0]}


# -----------------------------------------------------------------------------
# 覆盖记录的增删改
# -----------------------------------------------------------------------------
def upsert_override(
    session: Session,
    day: date,
    *,
    is_workday: bool,
    name: str = "",
    kind: str | None = None,
    source: str = "user_edit",
    note: str = "",
) -> WorkdayCalendar:
    """写入一天的覆盖。**同一天只能有一个说法。**"""
    if kind is None:
        if is_workday and is_weekend(day):
            kind = "makeup_workday"
        elif is_workday:
            kind = "custom_workday"
        else:
            kind = "holiday" if not is_weekend(day) else "custom_rest"
    row = session.scalar(select(WorkdayCalendar).where(WorkdayCalendar.day == day))
    if row is None:
        row = WorkdayCalendar(day=day)
        session.add(row)
    row.is_workday = bool(is_workday)
    row.kind = kind
    row.name = (name or "").strip()[:40]
    row.source = source
    row.note = (note or "").strip()[:200]
    session.flush()
    return row


def delete_override(session: Session, day: date) -> None:
    row = session.scalar(select(WorkdayCalendar).where(WorkdayCalendar.day == day))
    if row is not None:
        session.delete(row)
        session.flush()


def list_overrides(session: Session, *, year: int | None = None) -> list[WorkdayCalendar]:
    statement = select(WorkdayCalendar)
    if year is not None:
        statement = statement.where(
            WorkdayCalendar.day >= date(year, 1, 1),
            WorkdayCalendar.day <= date(year, 12, 31),
        )
    return list(session.scalars(statement.order_by(WorkdayCalendar.day)).all())


# -----------------------------------------------------------------------------
# 批量录入（官方发布的就是一份短列表）
# -----------------------------------------------------------------------------
#: `休 2026-01-01 元旦` / `班 2026-02-14` / `2026-01-01~2026-01-03 元旦`
_LINE = re.compile(
    r"^\s*(?P<mark>[休班假上]?)\s*"
    r"(?P<start>\d{4}-\d{1,2}-\d{1,2})"
    r"(?:\s*[~～至-]\s*(?P<end>\d{4}-\d{1,2}-\d{1,2}))?"
    r"\s*(?P<name>.*)$"
)


def _parse_day(text: str) -> date:
    parts = [int(part) for part in text.split("-")]
    if len(parts) != 3:
        raise ValueError(f"无法解析日期：{text}")
    return date(parts[0], parts[1], parts[2])


def parse_workday_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """解析用户粘贴的节假日安排。

    返回 ``(条目列表, 无法解析的行)``。

    **无法解析的行要原样返回给用户**，而不是静默跳过：
    他粘的是官方公告，如果有一行没被认出来，
    必须自己知道 —— 否则那个假期会悄悄变成工作日。
    """
    items: list[dict[str, Any]] = []
    bad: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            bad.append(raw.rstrip())
            continue
        try:
            start = _parse_day(match.group("start"))
            end = _parse_day(match.group("end") or match.group("start"))
        except ValueError:
            bad.append(raw.rstrip())
            continue
        if end < start:
            bad.append(raw.rstrip())
            continue
        if (end - start).days > 60:
            bad.append(raw.rstrip())
            continue
        # 没有标记时按"休"处理：官方公告里绝大多数条目是放假
        mark = match.group("mark") or "休"
        is_workday = mark in {"班", "上"}
        name = (match.group("name") or "").strip()[:40]
        cursor = start
        while cursor <= end:
            items.append({"day": cursor, "is_workday": is_workday, "name": name})
            cursor += timedelta(days=1)
    return items, bad


def import_workday_text(session: Session, text: str, *, source: str = "user_import") -> dict[str, Any]:
    """把粘贴的文本写进日历。返回写入条数与无法解析的行。"""
    items, bad = parse_workday_text(text)
    for item in items:
        upsert_override(
            session,
            item["day"],
            is_workday=item["is_workday"],
            name=item["name"],
            source=source,
        )
    return {"saved": len(items), "unparsed": bad}


# -----------------------------------------------------------------------------
# 工作日运算
# -----------------------------------------------------------------------------
def shift_to_workday(
    session: Session, day: date, policy: str, *, limit: int = _MAX_SHIFT_DAYS
) -> tuple[date, int]:
    """按策略把日期挪到工作日。返回 ``(日期, 移动了几天)``。

    `advance` 向前找（日期变小）—— "遇周末提前到最后一个工作日"；
    `postpone` 向后找 —— "顺延到下一个工作日"。
    `none` 不移动。
    """
    if policy not in POLICIES:
        raise ValidationError(f"未知的调整策略：{policy}", field="policy", allowed=list(POLICIES))
    if policy == "none" or is_workday(session, day):
        return day, 0
    step = -1 if policy == "advance" else 1
    cursor = day
    for offset in range(1, limit + 1):
        cursor = day + timedelta(days=step * offset)
        if is_workday(session, cursor):
            return cursor, step * offset
    raise ValidationError(
        f"{limit} 天内找不到工作日，请检查日历数据是否有误",
        field="workday_calendar",
        day=day.isoformat(),
    )


def nth_workday_of_month(session: Session, year: int, month: int, nth: int) -> date | None:
    """当月的第 N 个工作日。超出范围返回 ``None``。"""
    if nth < 1:
        return None
    total = monthrange(year, month)[1]
    seen = 0
    for day_number in range(1, total + 1):
        day = date(year, month, day_number)
        if is_workday(session, day):
            seen += 1
            if seen == nth:
                return day
    return None


def month_workdays(session: Session, year: int, month: int) -> list[date]:
    total = monthrange(year, month)[1]
    return [
        date(year, month, day_number)
        for day_number in range(1, total + 1)
        if is_workday(session, date(year, month, day_number))
    ]


# -----------------------------------------------------------------------------
# 发薪日
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class PaydayResolution:
    """一次发薪日计算的结果，**带上"为什么是这个日期"**。"""

    period: str
    #: 规则直接算出来的日期（月末 31 日、第 3 个工作日……）
    base_date: date
    #: 调整后的实际发薪日
    pay_date: date
    adjusted: bool
    #: 调整原因：'weekend' / 'holiday' / ''（没调整）
    reason: str
    #: 调整策略
    policy: str
    #: 移动了几天（负数=提前）
    shift_days: int
    #: 这一天涉及的非工作日名称（节假日名）
    holiday_name: str
    #: False 表示该年没有节假日数据，结果可能没算上法定假期
    confident: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "base_date": self.base_date.isoformat(),
            "pay_date": self.pay_date.isoformat(),
            "adjusted": self.adjusted,
            "reason": self.reason,
            "policy": self.policy,
            "shift_days": self.shift_days,
            "holiday_name": self.holiday_name,
            "confident": self.confident,
        }


def _base_date_for(session: Session, rule: Any, year: int, month: int) -> tuple[date, bool]:
    """按规则算出"未经调整"的发薪日。返回 ``(日期, 是否因月末收敛而调整)``。"""
    kind = getattr(rule, "day_kind", "fixed")
    total = monthrange(year, month)[1]
    if kind == "month_end":
        return date(year, month, total), False
    if kind == "last_workday":
        workdays = month_workdays(session, year, month)
        return (workdays[-1] if workdays else date(year, month, total)), False
    if kind == "nth_workday":
        found = nth_workday_of_month(session, year, month, int(getattr(rule, "nth", 1)))
        return (found or date(year, month, total)), found is None
    wanted = int(getattr(rule, "day_of_month", 15))
    if wanted >= 31 or wanted > total:
        # 31 日 / 不存在的日子一律收敛到月末 —— 这是用户最可能想要的，
        # 而"2 月没有 31 日所以跳过这个月"显然不是
        return date(year, month, total), True
    if wanted < 1:
        wanted = 1
    return date(year, month, wanted), False


def resolve_payday(session: Session, rule: Any, year: int, month: int) -> PaydayResolution:
    """算出某年某月的实际发薪日。

    周末与节假日**用不同策略**（规则上分开配置）：很多公司"遇周末提前、
    遇法定节假日也提前"，但也有"周末顺延、长假提前"的组合。
    用一套策略会在这种组合上直接给错日期。
    """
    base, clamped = _base_date_for(session, rule, year, month)
    period = f"{year:04d}-{month:02d}"

    fact = workday_fact(session, base)
    confident = fact.confident

    # 明确记录了调休上班 → 即便它是周末也照常发
    if fact.is_workday:
        return PaydayResolution(
            period=period,
            base_date=base,
            pay_date=base,
            adjusted=False,
            reason="",
            policy="none",
            shift_days=0,
            holiday_name="",
            confident=confident and not clamped,
        )

    # 非工作日：先看它是"周末"还是"法定节假日"，用对应的策略
    row = session.scalar(select(WorkdayCalendar).where(WorkdayCalendar.day == base))
    if row is not None and not row.is_workday and row.kind == "holiday":
        reason = "holiday"
        policy = getattr(rule, "holiday_policy", "advance")
        holiday_name = row.name
    else:
        reason = "weekend"
        policy = getattr(rule, "weekend_policy", "advance")
        holiday_name = row.name if row is not None else ""

    shifted, delta = shift_to_workday(session, base, policy)
    # 提前可能落进上一个月（例如 1 日遇周末提前到上月最后一个工作日）——
    # 这是真实存在的（有些公司就是上月发），因此不阻止，但要把期间留在本月，
    # 否则"1 月的工资"会被记成"12 月"
    return PaydayResolution(
        period=period,
        base_date=base,
        pay_date=shifted,
        adjusted=delta != 0,
        reason=reason,
        policy=policy,
        shift_days=delta,
        holiday_name=holiday_name,
        confident=confident and not clamped,
    )


def upcoming_paydays(
    session: Session, rules: Iterable[Any], *, start: date, months: int = 3
) -> list[dict[str, Any]]:
    """未来若干个月的发薪日（跨规则合并后按日期排序）。

    界面用它画"接下来什么时候发薪"，因此**必须先跨规则排序**再截断：
    按规则逐个列出会让"最近的一次发薪"淹没在列表里。
    """
    results: list[dict[str, Any]] = []
    year, month = start.year, start.month
    for _ in range(max(1, months)):
        for rule in rules:
            if not getattr(rule, "enabled", True):
                continue
            resolution = resolve_payday(session, rule, year, month)
            if resolution.pay_date < start:
                continue
            payload = resolution.as_dict()
            payload["rule_id"] = getattr(rule, "id", None)
            payload["source_id"] = getattr(rule, "source_id", None)
            results.append(payload)
        month += 1
        if month > 12:
            month = 1
            year += 1
    results.sort(key=lambda item: item["pay_date"])
    return results
