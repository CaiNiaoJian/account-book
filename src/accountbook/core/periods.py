"""期间与日期推算（纯函数）。

为什么单独抽一个模块
--------------------
"每月 31 号"在 2 月怎么办、"每年 2 月 29 日"在平年怎么办、
"每季度"从哪个月算起 —— 这些是**会算错且错了不报错**的地方。
把规则集中在这里、只写一遍，并由单元测试逐个钉住，
比在预算、周期记账、K 线三处各写一份可靠的得多
（三份实现迟早会有两份不一致，而用户会发现两个页面的月份不一样）。

约定
----
* 所有日期都是 naive 的 `date`，与账本的"业务时间"语义一致；
* 期间一律**左闭右闭**（含首尾两天），因为它要直接用来筛流水；
* ``clamp`` 是这里唯一允许"改天数"的地方，且必须在文档里写明。
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

__all__ = [
    "BUDGET_PERIODS",
    "add_months",
    "clamp_day",
    "iter_months",
    "normalize_period",
    "period_bounds",
    "period_key",
    "shift_period",
    "week_start",
]

#: 预算支持的期间。顺序即展示顺序
BUDGET_PERIODS = ("weekly", "monthly", "quarterly", "yearly")

#: 词形归一。
#:
#: 这里曾经埋着一个真实的 bug：``KlinePeriod`` 用的是 ``day/week/month/year``
#: （单数），``BudgetPeriod`` 用的是 ``weekly/monthly/quarterly/yearly``（形容词）。
#: 两套词形各自看起来都合理，但把它们混起来用时 ``period_bounds("week", ...)``
#: 会直接抛 ``ValueError``。与其在两处各写一层映射（迟早会漏一个），
#: 不如让本模块同时接受两种写法 —— 归一化只在这里做一次。
_PERIOD_ALIASES = {
    "day": "day",
    "daily": "day",
    "week": "weekly",
    "weekly": "weekly",
    "month": "monthly",
    "monthly": "monthly",
    "quarter": "quarterly",
    "quarterly": "quarterly",
    "year": "yearly",
    "yearly": "yearly",
    "custom": "custom",
}


def normalize_period(period: str) -> str:
    """把两种词形统一成 ``periods`` 内部使用的规范形式。"""
    try:
        return _PERIOD_ALIASES[period]
    except KeyError:
        raise ValueError(f"未知期间：{period}") from None


def clamp_day(year: int, month: int, day: int) -> date:
    """把"某月第 N 天"夹到该月真实存在的范围内。

    ``day`` 允许三个特殊值：

    * ``-1``：该月**最后一天**。这是"月底结账"的正确表达 ——
      直接写 31 会让 2 月、4 月、6 月、9 月、11 月永远不触发；
    * ``0``：与 ``-1`` 同义（历史写法，容忍它以免旧数据失效）；
    * 大于当月天数：夹到月末。用户在 31 号设了"每月 31 日还款"，
      2 月就该落在 28/29 日，而不是跳过这个月。

    夹取而不是跳过，是因为"这个月没还"对用户来说是更坏的结果。
    """
    last = monthrange(year, month)[1]
    if day in (-1, 0):
        return date(year, month, last)
    return date(year, month, min(max(day, 1), last))


def add_months(anchor: date, months: int) -> date:
    """按月平移，并把"日"夹到目标月的合法范围。

    用于"每月 N 号"：从 1 月 31 日加一个月得到 2 月 28/29 日，
    而不是抛 ``ValueError`` 或悄悄变成 3 月 2 日。
    """
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    return clamp_day(year, month + 1, anchor.day)


def week_start(anchor: date) -> date:
    """所在周的周一。**以周一为一周之首**（ISO 8601）。

    不用周日，是因为"周末消费"在周日开始的分周里会被劈成两半，
    而用户心里的"这周"是周一到周日。
    """
    return anchor - timedelta(days=anchor.weekday())


def period_bounds(period: str, anchor: date) -> tuple[date, date]:
    """``anchor`` 所在期间的起止日（左闭右闭）。

    ``custom`` 不在这里处理 —— 它的区间由用户直接给定，
    没有"推算"的成分，放进本模块只会让函数多一个永不使用的分支。
    """
    period = normalize_period(period)
    if period == "day":
        # 日线也要能走这条路：否则调用方得为"日"单独写一个分支，
        # 而那个分支迟早会与这里不一致
        return anchor, anchor
    if period == "weekly":
        start = week_start(anchor)
        return start, start + timedelta(days=6)
    if period == "monthly":
        return date(anchor.year, anchor.month, 1), date(
            anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1]
        )
    if period == "quarterly":
        first_month = (anchor.month - 1) // 3 * 3 + 1
        start = date(anchor.year, first_month, 1)
        end_month = first_month + 2
        return start, date(anchor.year, end_month, monthrange(anchor.year, end_month)[1])
    if period == "yearly":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    raise ValueError(f"未知期间：{period}")


def shift_period(period: str, anchor: date, steps: int) -> date:
    """把 ``anchor`` 平移 N 个期间（用于"上一期 / 下一期"）。"""
    period = normalize_period(period)
    if period == "day":
        return anchor + timedelta(days=steps)
    if period == "weekly":
        return anchor + timedelta(days=7 * steps)
    if period == "monthly":
        return add_months(anchor.replace(day=1), steps)
    if period == "quarterly":
        return add_months(anchor.replace(day=1), 3 * steps)
    if period == "yearly":
        # 2 月 29 日在平年会被夹到 2 月 28 日，这是可接受的近似
        return clamp_day(anchor.year + steps, anchor.month, anchor.day)
    raise ValueError(f"未知期间：{period}")


def period_key(period: str, anchor: date) -> str:
    """期间的稳定字符串标识（用于去重与缓存键）。"""
    start, _ = period_bounds(period, anchor)
    return f"{period}:{start.isoformat()}"


def iter_months(start: date, end: date) -> list[date]:
    """列出区间覆盖到的每个月的 1 号。"""
    cursor = date(start.year, start.month, 1)
    months: list[date] = []
    while cursor <= end:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    return months
