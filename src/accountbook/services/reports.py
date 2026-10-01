"""报表引擎（P5 / 需求 21）。

一份 Schema，三处消费
=====================
`ReportDocument` 是**唯一**的报告结构：界面用 `<ReportRenderer>` 渲染它、
导出用它生成 Markdown / HTML / PDF、外部 API 也返回它。
因此它必须与"怎么显示"解耦。

**后端只产出中性数据集，绝不产出 ECharts option。**
理由很实际：如果图表块里塞的是 ECharts 配置，那么每一个导出渲染器
（Markdown / HTML / PDF）都必须先"读懂 ECharts"才能画出任何东西 ——
而它们真正需要的是**数据**。同理，schema 一旦绑上某个图表库的版本，
换库就等于换 schema。
因此图表块给的是 `{chart: 'bar'|'line'|'pie'|'treemap'|..., dataset: {...}}`，
由各自的渲染器决定怎么画；导不出图时，同一个 dataset 直接落成一张表。

块（block）是唯一的扩展点
=========================
`text | metrics | table | chart | list | note | unavailable`。
新增一种展示只需要加一个块类型，而不是给每一节写一个新结构。

`unavailable` 块：诚实优先于好看
================================
P6 的薪酬与五险一金还没做。报表里**不放**一排 0 ——
那会让用户以为"我这个月五险一金是 0"，而事实是这个功能还不存在。
`unavailable` 块明确写出"这一节尚未实现（P6）"。
一个填满了假零的报告比一份缺了几节的报告危险得多。

同比与环比是两件事
==================
环比 = 与**紧邻的上一期**比；同比 = 与**去年同口径同期**比。
月度同比用"去年同月"，日/周同比按 **364 天**回退（恰好 52 周，
星期几不变）—— 按 365 天回退会让"本周"对上"去年的周一到周日"变成
"去年的周二到下周一"，那会让"周末消费高"这类结论完全失真。
"""

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..core.money import DEFAULT_CURRENCY
from ..core.periods import add_months
from ..db.models import Account, Category
from . import aggregate
from . import budgets as budgets_service
from . import insurance as insurance_service
from . import kline as kline_service
from . import ledger as ledger_service
from . import piggy as piggy_service
from .payroll import list_records as list_payroll_records

__all__ = [
    "KINDS",
    "SCHEMA_VERSION",
    "build_report",
    "resolve_period",
    "section_keys",
]

_logger = logging.getLogger(__name__)

#: schema 版本。外部 API 消费它时需要能判断结构是否变了
SCHEMA_VERSION = "accountbook.report/1"

KINDS = ("daily", "weekly", "monthly", "yearly", "custom")

#: 一期最多多少天。年报 366 天没问题，但"自定义区间"必须有个上限，
#: 否则一次请求能把整库拉出来
MAX_CUSTOM_DAYS = 400

#: 一条洞察最多带几条证据
_MAX_EVIDENCE = 5


# -----------------------------------------------------------------------------
# 期间
# -----------------------------------------------------------------------------
def resolve_period(
    kind: str, *, start: date | None = None, end: date | None = None, today: date | None = None
) -> tuple[date, date]:
    """把"报告类型"解析成具体区间。

    ``custom`` 必须给出 ``start``/``end``；其余类型以 ``today`` 为锚点
    自动定位**当前**这一期（日报 = 今天，周报 = 本周一至周日，等等）。
    """
    if kind not in KINDS:
        raise ValidationError(f"未知的报告类型：{kind}", field="kind", allowed=list(KINDS))
    today = today or datetime.now().date()

    if kind == "custom":
        if start is None or end is None:
            raise ValidationError("自定义报表必须给出起止日期", field="start")
        if end < start:
            raise ValidationError("结束日期不能早于起始日期", field="end")
        if (end - start).days + 1 > MAX_CUSTOM_DAYS:
            raise ValidationError(
                f"自定义区间最多 {MAX_CUSTOM_DAYS} 天", field="end", max_days=MAX_CUSTOM_DAYS
            )
        return start, end

    if kind == "daily":
        return today, today
    if kind == "weekly":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if kind == "monthly":
        first = today.replace(day=1)
        last = today.replace(day=monthrange(today.year, today.month)[1])
        return first, last
    # yearly
    return today.replace(month=1, day=1), today.replace(month=12, day=31)


def _shift_year_back(start: date, end: date, kind: str) -> tuple[date, date]:
    """同比区间。

    月度/年度按**日历年**回退（"去年 10 月"就是去年 10 月），
    日/周按 **364 天**回退 —— 恰好 52 周，星期几保持不变。
    按 365 天回退会让"本周"对上"去年的周二到下周一"，
    于是"周末消费高"这类结论会完全失真。
    """
    if kind in {"daily", "weekly"}:
        return start - timedelta(days=364), end - timedelta(days=364)
    return add_months(start, -12), add_months(end, -12)


def _previous_period(start: date, end: date) -> tuple[date, date]:
    """环比区间：紧邻上一期，**等长**。"""
    span = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    return previous_end - timedelta(days=span - 1), previous_end


def _label(kind: str, start: date, end: date) -> str:
    if kind == "daily":
        return start.isoformat()
    if kind == "monthly":
        return f"{start.year} 年 {start.month} 月"
    if kind == "yearly":
        return f"{start.year} 年"
    if kind == "weekly":
        return f"{start.isoformat()} 起的一周"
    return f"{start.isoformat()} — {end.isoformat()}"


# -----------------------------------------------------------------------------
# 块构造器
# -----------------------------------------------------------------------------
def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _note(text: str) -> dict[str, Any]:
    """口径说明。导出与界面都会把它排成更淡的样式。"""
    return {"type": "note", "text": text}


def _unavailable(reason: str) -> dict[str, Any]:
    """这一节尚未实现。**明确写出来，而不是放一排 0。**"""
    return {"type": "unavailable", "text": reason}


def _metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "metrics", "items": items}


def _table(
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    *,
    title: str = "",
    footer: dict[str, Any] | None = None,
    empty: str = "这一期没有数据",
) -> dict[str, Any]:
    return {
        "type": "table",
        "title": title,
        "columns": columns,
        "rows": rows,
        # 空表要带一句说明：一张只有表头的表会让人以为加载失败
        "empty": empty,
        "footer": footer,
    }


def _chart(chart: str, dataset: dict[str, Any], *, title: str = "", unit: str = "money") -> dict[str, Any]:
    """图表块。**只给中性数据集，不给任何图表库的配置**（见模块 docstring）。

    `unit` 让渲染器知道怎么格式化数字（金额 / 百分比 / 计数），
    而不用去猜字段名。
    """
    return {"type": "chart", "chart": chart, "title": title, "dataset": dataset, "unit": unit}


def _money_metric(
    key: str,
    label: str,
    value_minor: int,
    *,
    delta_ratio: float | None = None,
    delta_label: str = "",
    tone: str = "neutral",
    hint: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": "money",
        "value_minor": value_minor,
        "delta_ratio": delta_ratio,
        "delta_label": delta_label,
        "tone": tone,
        "hint": hint,
    }


def _count_metric(key: str, label: str, value: int, *, hint: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": "count",
        "value": value,
        "delta_ratio": None,
        "delta_label": "",
        "tone": "neutral",
        "hint": hint,
    }


def _ratio_delta(current: int, previous: int) -> float | None:
    """变化率。上一期为 0 时返回 ``None`` 而不是 0 或无穷大。

    "从 0 涨到 100"没有可表达的百分比；返回 0 会谎称"没有变化"，
    返回无穷大又没法显示。`None` 让渲染器显示"—"。
    """
    if previous == 0:
        return None
    return (current - previous) / abs(previous)


# -----------------------------------------------------------------------------
# 各节
# -----------------------------------------------------------------------------
def _period_totals(session: Session, start: date, end: date) -> dict[str, int]:
    """区间内的收入 / 支出 / 净额。转账与调整不计入（与全局口径一致）。"""
    income = 0
    expense = 0
    for row in aggregate.load_transactions(session, start=start, end=end, types={"income", "expense"}):
        if row.type == "income":
            income += row.amount_minor
        else:
            expense += row.amount_minor
    return {"income_minor": income, "expense_minor": expense, "net_minor": income - expense}


def _section_overview(
    session: Session, start: date, end: date, kind: str, totals: dict[str, int]
) -> dict[str, Any]:
    previous_start, previous_end = _previous_period(start, end)
    previous = _period_totals(session, previous_start, previous_end)
    year_start, year_end = _shift_year_back(start, end, kind)
    year_ago = _period_totals(session, year_start, year_end)

    days = (end - start).days + 1
    by_day = aggregate.spending_by_day(session, start=start, end=end)
    daily = [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "value_minor": by_day.get(start + timedelta(days=offset), 0),
        }
        for offset in range(days)
    ]
    # 只统计有记录的天数：用总天数当分母会把"这一期只记了 3 天"算成日均很低
    active_days = sum(1 for item in daily if item["value_minor"] > 0)

    return {
        "key": "overview",
        "title": "收支概览",
        "blocks": [
            _metrics(
                [
                    _money_metric(
                        "income",
                        "收入",
                        totals["income_minor"],
                        delta_ratio=_ratio_delta(totals["income_minor"], previous["income_minor"]),
                        delta_label="环比",
                        tone="positive",
                        hint=f"同比 {_format_delta(_ratio_delta(totals['income_minor'], year_ago['income_minor']))}",
                    ),
                    _money_metric(
                        "expense",
                        "支出",
                        totals["expense_minor"],
                        delta_ratio=_ratio_delta(totals["expense_minor"], previous["expense_minor"]),
                        delta_label="环比",
                        tone="negative",
                        hint=f"同比 {_format_delta(_ratio_delta(totals['expense_minor'], year_ago['expense_minor']))}",
                    ),
                    _money_metric(
                        "net",
                        "净额",
                        totals["net_minor"],
                        delta_ratio=_ratio_delta(totals["net_minor"], previous["net_minor"]),
                        delta_label="环比",
                        tone="positive" if totals["net_minor"] >= 0 else "negative",
                    ),
                    _money_metric(
                        "expense_per_active_day",
                        "有记录日均支出",
                        round(totals["expense_minor"] / active_days) if active_days else 0,
                        hint=f"共 {active_days} 天有记录 / 区间 {days} 天",
                    ),
                ]
            ),
            _chart("bar", {"points": daily}, title="每日支出", unit="money"),
            _table(
                [
                    {"key": "label", "label": "口径", "align": "left"},
                    {"key": "income_minor", "label": "收入", "align": "right", "format": "money"},
                    {"key": "expense_minor", "label": "支出", "align": "right", "format": "money"},
                    {"key": "net_minor", "label": "净额", "align": "right", "format": "money"},
                ],
                [
                    {"label": "本期", **totals},
                    {"label": "上一期（环比）", **previous},
                    {"label": "去年同期（同比）", **year_ago},
                ],
                title="环比与同比",
            ),
        ],
    }


def _format_delta(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    return f"{ratio * 100:+.1f}%"


def _category_names(session: Session, ids: set[int]) -> dict[int, str]:
    """一次查出分类名，避免按行查询（N+1）。"""
    if not ids:
        return {}
    return {
        item.id: item.name for item in session.scalars(select(Category).where(Category.id.in_(ids))).all()
    }


def _breakdown_rows(session: Session, start: date, end: date, kind: str) -> tuple[list[dict[str, Any]], int]:
    """分类明细 + 合计。

    `category_totals` 返回的是 ``[(分类 id, 金额)]`` 元组，**已按金额降序**；
    分类名要自己解析。``category_id`` 为 ``None`` 是一档真实存在的"未分类" ——
    合并进"其它"会掩盖"我有多少笔没分类"，而那正是用户需要看到的。
    """
    raw = aggregate.category_totals(session, start=start, end=end, kind=kind)
    total = sum(int(amount) for _, amount in raw)
    names = _category_names(session, {int(cid) for cid, _ in raw if cid is not None})
    rows: list[dict[str, Any]] = []
    for category_id, amount in raw:
        value = int(amount)
        rows.append(
            {
                "name": names.get(int(category_id), "未分类") if category_id is not None else "未分类",
                "category_id": category_id,
                "total_minor": value,
                # 占比在这里算好：让每个渲染器各算一遍必然会有一处算错
                "share": (value / total) if total else 0,
            }
        )
    return rows, total


def _section_breakdown(session: Session, start: date, end: date) -> dict[str, Any]:
    expense_detail, expense_total = _breakdown_rows(session, start, end, "expense")
    income_detail, income_total = _breakdown_rows(session, start, end, "income")
    # 只取前 8 名画图：饼图超过 8 块就只能看颜色了，明细表里仍然是全部
    top_expense = expense_detail[:8]

    return {
        "key": "breakdown",
        "title": "结构分解",
        "blocks": [
            _metrics(
                [
                    _count_metric("expense_categories", "支出涉及分类", len(expense_detail)),
                    _count_metric("income_categories", "收入涉及分类", len(income_detail)),
                    _money_metric(
                        "top_expense",
                        "最大单项支出",
                        expense_detail[0]["total_minor"] if expense_detail else 0,
                        hint=expense_detail[0]["name"] if expense_detail else "无支出",
                    ),
                ]
            ),
            _chart("pie", {"points": top_expense}, title="支出构成（前 8）", unit="money"),
            _table(
                [
                    {"key": "name", "label": "分类", "align": "left"},
                    {"key": "total_minor", "label": "金额", "align": "right", "format": "money"},
                    {"key": "share", "label": "占比", "align": "right", "format": "percent"},
                ],
                expense_detail,
                title="支出明细",
                footer={"name": "合计", "total_minor": expense_total, "share": 1.0 if expense_total else 0},
                empty="这一期没有支出",
            ),
            _table(
                [
                    {"key": "name", "label": "分类", "align": "left"},
                    {"key": "total_minor", "label": "金额", "align": "right", "format": "money"},
                    {"key": "share", "label": "占比", "align": "right", "format": "percent"},
                ],
                income_detail,
                title="收入明细",
                footer={"name": "合计", "total_minor": income_total, "share": 1.0 if income_total else 0},
                empty="这一期没有收入",
            ),
            _note(
                "口径：支出按**分账优先**归集 —— 一笔流水若拆了分账，"
                "金额算在各分账的分类上而不是主分类，因此这里的合计等于实际支出总额。"
                "转账与余额调整不计入收支。"
            ),
        ],
    }


def _section_accounts(session: Session, start: date, end: date) -> dict[str, Any]:
    """账户与卡片变动。复用台账服务 —— 它就是为"算清一个账户"而写的。"""
    accounts = list(session.scalars(select(Account).where(Account.deleted_at.is_(None))).all())
    rows: list[dict[str, Any]] = []
    for account in accounts:
        opening = ledger_service.opening_balance(session, account.id, start)
        closing = ledger_service.balance_as_of(session, account.id, end)
        rows.append(
            {
                "name": account.name,
                "kind": account.type,
                "opening_minor": opening,
                "closing_minor": closing,
                "change_minor": closing - opening,
            }
        )
    # 有变动的排前面；全是 0 变动的账户堆在最上面会让这一节看起来没内容
    rows.sort(key=lambda item: (-abs(int(item["change_minor"])), str(item["name"])))
    total_change = sum(int(row["change_minor"]) for row in rows)

    return {
        "key": "accounts",
        "title": "账户与卡片变动",
        "blocks": [
            _metrics(
                [
                    _count_metric("account_count", "账户数", len(rows)),
                    _money_metric(
                        "total_change",
                        "净值变动",
                        total_change,
                        tone="positive" if total_change >= 0 else "negative",
                    ),
                ]
            ),
            _table(
                [
                    {"key": "name", "label": "账户", "align": "left"},
                    {"key": "opening_minor", "label": "期初", "align": "right", "format": "money"},
                    {"key": "change_minor", "label": "变动", "align": "right", "format": "money"},
                    {"key": "closing_minor", "label": "期末", "align": "right", "format": "money"},
                ],
                rows,
                title="分账户明细",
                footer={"name": "合计", "change_minor": total_change},
                empty="没有账户",
            ),
            _note(
                "口径：期初 = 账户起点余额 + 起始日之前的全部流水；"
                "期末 = 截至区间末的余额。与台账页用的是同一套算法，"
                "因此这里的数字和台账对得上。"
            ),
        ],
    }


def _section_budgets(session: Session, end: date) -> dict[str, Any]:
    overview = budgets_service.overview(session, on=end)
    rows: list[dict[str, Any]] = []
    for item in overview.get("items", []):
        rows.append(
            {
                "name": item.get("name", ""),
                "period": item.get("period", ""),
                "budget_minor": int(item.get("amount_minor", 0)),
                "spent_minor": int(item.get("spent_minor", 0)),
                "remaining_minor": int(item.get("remaining_minor", 0)),
                "ratio": float(item.get("ratio", 0)),
            }
        )
    blocks: list[dict[str, Any]] = []
    if not rows:
        blocks.append(_text("还没有设置任何预算。设一条预算之后，这里会显示执行进度。"))
    else:
        blocks.append(
            _table(
                [
                    {"key": "name", "label": "预算", "align": "left"},
                    {"key": "budget_minor", "label": "额度", "align": "right", "format": "money"},
                    {"key": "spent_minor", "label": "已用", "align": "right", "format": "money"},
                    {"key": "remaining_minor", "label": "剩余", "align": "right", "format": "money"},
                    {"key": "ratio", "label": "进度", "align": "right", "format": "percent"},
                ],
                rows,
                title="预算执行",
            )
        )
    blocks.append(
        _note(
            "口径：跨期预算（年度 / 季度）按**整期**统计已用金额，"
            "因此它的「已用」可能大于本期区间内的支出 —— 这是预算本身的口径，不是错误。"
        )
    )
    return {"key": "budgets", "title": "预算执行", "blocks": blocks}


def _section_piggy(session: Session) -> dict[str, Any]:
    banks = piggy_service.list_banks(session)
    if not banks:
        return {
            "key": "piggy",
            "title": "存钱罐进度",
            "blocks": [_text("还没有存钱罐。建一个罐子之后，这里会显示进度与预计达成日。")],
        }
    balances = piggy_service._sum_all_balances(session, [bank.id for bank in banks])
    rows: list[dict[str, Any]] = []
    for bank in banks:
        current = balances.get(bank.id, 0)
        eta = piggy_service.estimate_completion(session, bank, balance_minor=current)
        rows.append(
            {
                "name": bank.name,
                "target_name": bank.target_name,
                "balance_minor": current,
                "target_amount_minor": bank.target_amount_minor,
                "ratio": piggy_service.progress(current, bank.target_amount_minor),
                "status": bank.status,
                # 两个口径都给：报表是"留档"的，只留一个会让以后无法复算
                "eta_linear": (eta.get("linear") or {}).get("eta", ""),
                "eta_weighted": (eta.get("weighted") or {}).get("eta", ""),
            }
        )
    rows.sort(key=lambda item: -float(item["ratio"]))
    return {
        "key": "piggy",
        "title": "存钱罐进度",
        "blocks": [
            _chart(
                "bar",
                {"points": [{"name": row["name"], "value": row["ratio"]} for row in rows[:10]]},
                title="完成比例（前 10）",
                unit="percent",
            ),
            _table(
                [
                    {"key": "name", "label": "罐子", "align": "left"},
                    {"key": "balance_minor", "label": "已攒", "align": "right", "format": "money"},
                    {"key": "target_amount_minor", "label": "目标", "align": "right", "format": "money"},
                    {"key": "ratio", "label": "进度", "align": "right", "format": "percent"},
                    {"key": "eta_weighted", "label": "预计达成（加权）", "align": "right"},
                ],
                rows,
                title="罐子明细",
            ),
            _note(
                "预计达成日给两个口径：线性（累计 ÷ 已过天数）稳定但对「最近停止存钱了」无感；"
                "加权（近 90 天按 15 天半衰期）对节奏变化敏感。两者分歧超过一倍时，"
                "以后者更接近当前实际情况。"
            ),
        ],
    }


def _section_kline(session: Session, start: date, end: date) -> dict[str, Any]:
    payload = kline_service.series(session, period="day", start=start, end=end, indicators=False)
    bars = payload.get("bars", [])
    closes = [float(bar["close_minor"]) for bar in bars]
    if not closes:
        return {
            "key": "trend",
            "title": "净值趋势与回撤",
            "blocks": [
                _text(
                    "这一期还没有净值数据。净资产曲线会在记满第一笔流水之后出现 ——"
                    "在此之前没有「期初净值」可画。"
                )
            ],
        }
    drawdowns = kline_service.drawdown(closes)
    points = [
        {
            "date": bar["period_start"],
            "value_minor": int(bar["close_minor"]),
            "drawdown": drawdowns[index],
        }
        for index, bar in enumerate(bars)
    ]
    worst = min(drawdowns) if drawdowns else 0.0
    peak_ratio = (closes[-1] / closes[0] - 1) if closes[0] else 0.0
    return {
        "key": "trend",
        "title": "净值趋势与回撤",
        "blocks": [
            _metrics(
                [
                    _money_metric("closing_net_worth", "期末净值", int(closes[-1])),
                    {
                        "key": "period_return",
                        "label": "期内变化",
                        "kind": "percent",
                        "value": peak_ratio,
                        "delta_ratio": None,
                        "delta_label": "",
                        "tone": "positive" if peak_ratio >= 0 else "negative",
                        "hint": "",
                    },
                    {
                        "key": "max_drawdown",
                        "label": "最大回撤",
                        "kind": "percent",
                        "value": worst,
                        "delta_ratio": None,
                        "delta_label": "",
                        "tone": "negative",
                        "hint": "净值从峰值回落的最大幅度",
                    },
                ]
            ),
            _chart("line", {"points": points, "value_key": "value_minor"}, title="净值曲线", unit="money"),
            _chart("line", {"points": points, "value_key": "drawdown"}, title="回撤曲线", unit="percent"),
            _note(
                "口径：净值 = 全部账户余额之和，由流水聚合而来（余额不落库）。"
                "回撤 = 净值相对**历史峰值**的回落幅度。"
                "这里说的是你的现金流，不是投资建议。"
            ),
        ],
    }


def _section_calendar(session: Session, start: date, end: date) -> dict[str, Any]:
    days = (end - start).days + 1
    by_day = aggregate.spending_by_day(session, start=start, end=end)
    # 只把"今天或之前"算进完整度：把未来还没到的日子算成"漏记"是在冤枉用户
    today = datetime.now().date()
    countable = [start + timedelta(days=offset) for offset in range(days)]
    countable = [day for day in countable if day <= today]
    recorded = [day for day in countable if by_day.get(day, 0) > 0]
    missing = [day for day in countable if by_day.get(day, 0) <= 0]
    ratio = (len(recorded) / len(countable)) if countable else 0.0
    return {
        "key": "calendar",
        "title": "登记完整度",
        "blocks": [
            _metrics(
                [
                    _count_metric("recorded_days", "有记录的天数", len(recorded)),
                    _count_metric("missing_days", "没有记录的天数", len(missing)),
                    {
                        "key": "completeness",
                        "label": "完整度",
                        "kind": "percent",
                        "value": ratio,
                        "delta_ratio": None,
                        "delta_label": "",
                        "tone": "positive" if ratio >= 0.8 else "neutral",
                        "hint": "",
                    },
                ]
            ),
            _note(
                "口径：只统计**今天及之前**的日子。未来的日期算成「漏记」是在冤枉用户。"
                "「有记录」指当天至少有一笔支出 —— 只有收入的日子不会被算作已登记支出。"
            ),
        ],
    }


def _periods_in(start: date, end: date) -> list[str]:
    """区间覆盖到的月份（含两端）。

    报告区间是按天的（`2026-09-11 — 2026-10-01`），而工资与缴纳是按月的。
    直接用 start 的月份会漏掉 10 月那部分；用自然月又会把没覆盖的日子算进来。
    这里取**两端之间的所有月份**，宁可多算一个月也不漏 ——
    漏掉的那个月正是用户最可能想看的（比如刚发的这次工资）。
    """
    periods: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return periods


def _section_payroll(session: Session, start: date, end: date) -> dict[str, Any]:
    """P6 的薪酬与五险一金。

    这一节在 P5 时是 `unavailable` 块（"尚未实现，不放一排 0"）。
    现在数据齐了，于是它变成真实内容 —— 但**空状态仍要与"未实现"区分**：
    没记录时说"录一次工资就有了"，而不是说"功能还没做"。

    口径上与别处的三处一致：
    * 入账金额用**实发**（账户里真正到账的）；
    * 单位缴纳单列，因为它是没进工资卡但确实属于你的"隐形收入"；
    * 作废/跳过的记录不计入合计。
    """
    periods = _periods_in(start, end)
    all_records = list_payroll_records(session, limit=1000)
    records = [
        row
        for row in all_records
        if row.period in periods and row.status != "skipped" and row.deleted_at is None
    ]
    insurance = insurance_service.insurance_overview(session, start_period=periods[0], end_period=periods[-1])
    pending = [row for row in all_records if row.status == "draft" and row.period in periods]

    blocks: list[dict[str, Any]] = []

    if not records and insurance["record_count"] == 0:
        blocks.append(
            _text(
                "这一区间还没有工资或五险一金记录。"
                "录入一次工资（或设置发薪规则让系统按月提醒）之后，"
                "这里会显示应发实发构成、五险一金明细与账户积累。"
            )
        )
        blocks.append(
            _note(
                "五险一金的**比例因城市与年份而异**，系统不会预置 —— "
                "请在设置里按当地政策填写，否则合计会是 0。"
            )
        )
        return {"key": "payroll", "title": "薪酬与五险一金", "blocks": blocks}

    gross = sum(row.gross_minor for row in records)
    net = sum(row.net_minor for row in records)
    tax = sum(row.tax_minor for row in records)
    deducted_insurance = sum(int(row.insurance_snapshot.get("total_minor", 0)) for row in records)
    # 工资表里的其它减项（例如税前扣除）—— 用它把瀑布拼平
    other_deduction = gross - net - tax - deducted_insurance

    blocks.append(
        _metrics(
            [
                _money_metric("payroll_gross", "应发合计", gross, hint=f"{len(records)} 条记录"),
                _money_metric("payroll_net", "实发合计", net, tone="positive"),
                _money_metric("payroll_tax", "个税", tax, tone="negative"),
                _money_metric("payroll_insurance", "五险一金代扣", deducted_insurance, tone="negative"),
                _money_metric(
                    "payroll_employer",
                    "单位缴纳（隐形收入）",
                    insurance["employer_total_minor"],
                    tone="positive",
                    hint="没进工资卡，但确实是你的",
                ),
            ]
        )
    )

    # 应发实发瀑布。用带符号的柱：负值是减项，前端与导出都能画出来
    waterfall = [
        {"name": "应发", "value_minor": gross},
        {"name": "五险一金", "value_minor": -deducted_insurance},
    ]
    if tax:
        waterfall.append({"name": "个税", "value_minor": -tax})
    if other_deduction:
        waterfall.append({"name": "其它扣除", "value_minor": -other_deduction})
    waterfall.append({"name": "实发", "value_minor": net})
    blocks.append(_chart("bar", {"points": waterfall}, title="应发 → 实发", unit="money"))

    # 逐月工资
    by_period: dict[str, dict[str, Any]] = {}
    for row in records:
        bucket = by_period.setdefault(
            row.period,
            {
                "period": row.period,
                "gross_minor": 0,
                "tax_minor": 0,
                "insurance_minor": 0,
                "net_minor": 0,
                "count": 0,
            },
        )
        bucket["gross_minor"] += row.gross_minor
        bucket["tax_minor"] += row.tax_minor
        bucket["insurance_minor"] += int(row.insurance_snapshot.get("total_minor", 0))
        bucket["net_minor"] += row.net_minor
        bucket["count"] += 1
    monthly = [by_period[key] for key in sorted(by_period)]
    blocks.append(
        _table(
            [
                {"key": "period", "label": "期间", "align": "left"},
                {"key": "gross_minor", "label": "应发", "align": "right", "format": "money"},
                {"key": "insurance_minor", "label": "五险一金", "align": "right", "format": "money"},
                {"key": "tax_minor", "label": "个税", "align": "right", "format": "money"},
                {"key": "net_minor", "label": "实发", "align": "right", "format": "money"},
            ],
            monthly,
            title="逐月工资",
            footer={
                "period": "合计",
                "gross_minor": gross,
                "insurance_minor": deducted_insurance,
                "tax_minor": tax,
                "net_minor": net,
            },
        )
    )

    # 五险一金构成（单位 vs 个人）
    if insurance["by_kind"]:
        blocks.append(
            _table(
                [
                    {"key": "name", "label": "险种", "align": "left"},
                    {"key": "personal_minor", "label": "个人", "align": "right", "format": "money"},
                    {"key": "employer_minor", "label": "单位", "align": "right", "format": "money"},
                ],
                insurance["by_kind"],
                title="五险一金构成",
                footer={
                    "name": "合计",
                    "personal_minor": insurance["personal_total_minor"],
                    "employer_minor": insurance["employer_total_minor"],
                },
            )
        )
        blocks.append(
            _chart(
                "pie",
                {
                    "points": [
                        {"name": item["name"], "value_minor": item["personal_minor"] + item["employer_minor"]}
                        for item in insurance["by_kind"]
                    ]
                },
                title="五险一金构成",
                unit="money",
            )
        )

    if insurance["account_total_minor"]:
        blocks.append(
            _metrics(
                [
                    _money_metric(
                        "insurance_account_total", "个人账户余额", insurance["account_total_minor"]
                    ),
                    _money_metric(
                        "insurance_account_housing",
                        "其中公积金",
                        insurance["account_balances"].get("housing_fund", 0),
                    ),
                    _money_metric(
                        "insurance_account_pension",
                        "其中养老",
                        insurance["account_balances"].get("pension", 0),
                    ),
                ]
            )
        )

    if pending:
        # 未填写的记录要点出来：它们会让"这个月实发"看起来偏低
        blocks.append(
            _note(f"另有 {len(pending)} 条草稿记录尚未填写，未计入上面的合计 —— 填完之后数字会变。")
        )

    # 有险种还没填比例时，合计**必然偏小** —— 不说明会让用户以为缴得少
    if insurance["record_count"] and not insurance["personal_total_minor"]:
        blocks.append(
            _note(
                "五险一金的缴纳记录存在，但合计为 0 —— 很可能是险种比例还没填。"
                "比例因城市与年份而异，需要按当地政策录入。"
            )
        )

    blocks.append(
        _note(
            "口径：入账金额用**实发**（账户里真正到账的）；作废与跳过的记录不计入合计；"
            "单位缴纳单列，因为它没进工资卡。利息由你在年度对账里录入 —— "
            "利率因城市与年份而异，系统不预置。"
        )
    )
    return {"key": "payroll", "title": "薪酬与五险一金", "blocks": blocks}


def _section_insights(session: Session, start: date, end: date, totals: dict[str, int]) -> dict[str, Any]:
    """规则驱动的洞察（离线，不依赖任何外部服务）。"""
    insights: list[dict[str, Any]] = []

    # 净流出
    if totals["net_minor"] < 0:
        expense_share = totals["expense_minor"] / totals["income_minor"] if totals["income_minor"] else None
        insights.append(
            {
                "key": "negative_net",
                "level": "warn",
                "title": "这一期是净流出",
                "detail": "支出超过收入，差额从既有余额里补。",
                "evidence": [f"收入 {totals['income_minor']} / 支出 {totals['expense_minor']}（最小单位）"],
                "suggestion": "看一下支出明细里的前两项，通常是它们拉高了总额。",
                "confidence": 1.0,
            }
        )
        if expense_share is not None and expense_share > 1.5:
            insights.append(
                {
                    "key": "expense_ratio",
                    "level": "critical",
                    "title": "支出是收入的 1.5 倍以上",
                    "detail": "这个比例若持续，余额会快速下降。",
                    "evidence": [f"支出/收入 = {expense_share:.2f}"],
                    "suggestion": "先确认有没有一次性大额支出；若没有，需要下调固定开支。",
                    "confidence": 0.9,
                }
            )

    # 分类突增（与上一期比）
    previous_start, previous_end = _previous_period(start, end)
    # 复用 `_breakdown_rows` 而不是自己再拼一遍：`category_totals` 返回的是
    # `[(分类 id, 金额)]` 元组，两处各写一次"元组 → 带名字的字典"的转换，
    # 就必然有一处会写成 `row["name"]`（我第一版就是这么错的）。
    current_detail, _ = _breakdown_rows(session, start, end, "expense")
    previous_detail, _ = _breakdown_rows(session, previous_start, previous_end, "expense")
    current_rows = {row["name"]: int(row["total_minor"]) for row in current_detail}
    previous_rows = {row["name"]: int(row["total_minor"]) for row in previous_detail}
    spikes: list[tuple[str, float, int, int]] = []
    for name, amount in current_rows.items():
        before = previous_rows.get(name, 0)
        # 上一期太小的时候不算比例："从 5 元涨到 50 元"是 10 倍但没有意义
        if before >= 10_000 and amount > before * 1.5:
            spikes.append((name, amount / before, amount, before))
    for name, ratio, amount, before in sorted(spikes, key=lambda item: -item[1])[:3]:
        insights.append(
            {
                "key": f"spike:{name}",
                "level": "info",
                "title": f"「{name}」比上一期高出 {(ratio - 1) * 100:.0f}%",
                "detail": "这一类支出的增幅明显高于其它分类。",
                "evidence": [f"本期 {amount} / 上期 {before}（最小单位）"],
                "suggestion": "如果这是一次性的，可以忽略；如果是常态，值得单独设一条预算。",
                "confidence": 0.7,
            }
        )

    # 大额单笔
    biggest = None
    for row in aggregate.load_transactions(session, start=start, end=end, types={"expense"}):
        if biggest is None or row.amount_minor > biggest.amount_minor:
            biggest = row
    if biggest is not None and totals["expense_minor"] > 0:
        share = biggest.amount_minor / totals["expense_minor"]
        if share >= 0.25:
            insights.append(
                {
                    "key": "single_large",
                    "level": "info",
                    "title": "单笔支出占了本期支出的 1/4 以上",
                    "detail": f"{(biggest.payee or biggest.note or '一笔支出')}",
                    "evidence": [
                        f"金额 {biggest.amount_minor}（占 {share * 100:.0f}%）",
                        f"日期 {biggest.occurred_at.date().isoformat()}",
                    ],
                    "suggestion": "大额支出单列出来看，比混在分类里更容易判断是否值得。",
                    "confidence": 0.8,
                }
            )

    # 登记完整度
    days = (end - start).days + 1
    today = datetime.now().date()
    countable = [
        start + timedelta(days=offset) for offset in range(days) if (start + timedelta(days=offset)) <= today
    ]
    by_day = aggregate.spending_by_day(session, start=start, end=end)
    missing = [day for day in countable if by_day.get(day, 0) <= 0]
    if countable and len(missing) / len(countable) > 0.5 and len(countable) >= 5:
        insights.append(
            {
                "key": "calendar_gap",
                "level": "info",
                "title": "这一期过半的日子没有支出记录",
                "detail": "可能确实没有消费，也可能只是忘了记 —— 两种情况对报表的影响完全不同。",
                "evidence": [f"{len(missing)} / {len(countable)} 天没有支出记录"],
                "suggestion": "如果只是忘了记，报表里的「日均支出」会偏高，别拿它当依据。",
                "confidence": 0.6,
            }
        )

    # 预算超支
    overview = budgets_service.overview(session, on=end)
    for item in overview.get("items", []):
        ratio = float(item.get("ratio", 0))
        if ratio > 1:
            insights.append(
                {
                    "key": f"budget_over:{item.get('id')}",
                    "level": "warn",
                    "title": f"预算「{item.get('name', '')}」已超支",
                    "detail": "已用金额超过了额度。",
                    "evidence": [f"进度 {ratio * 100:.0f}%"],
                    "suggestion": "要么调高额度，要么在下个周期减少这一类支出。",
                    "confidence": 0.95,
                }
            )

    # 存钱罐落后
    for bank in piggy_service.list_banks(session, status="active"):
        if bank.deadline is None:
            continue
        eta = piggy_service.estimate_completion(session, bank)
        if eta.get("achieved") or eta.get("on_track") is not False:
            continue
        insights.append(
            {
                "key": f"piggy_behind:{bank.id}",
                "level": "info",
                "title": f"存钱罐「{bank.name}」按当前速度赶不上目标日期",
                "detail": "加权口径的预计达成日晚于设定的目标日期。",
                "evidence": [
                    f"目标日期 {bank.deadline.isoformat()}",
                    f"还需每天 {eta.get('required_per_day_minor', 0)}（最小单位）",
                ],
                "suggestion": "要么调整目标日期，要么提高自动归集的比例。",
                "confidence": 0.7,
            }
        )

    if not insights:
        insights.append(
            {
                "key": "nothing_notable",
                "level": "good",
                "title": "这一期没有发现需要关注的地方",
                "detail": "收支结构、预算执行与登记完整度都在合理范围内。",
                "evidence": [],
                "suggestion": "",
                "confidence": 0.8,
            }
        )

    return {
        "key": "insights",
        "title": "异常与亮点",
        "blocks": [_text("以下结论由**离线规则**得出，没有联网、也没有调用任何模型。")],
        "insights": insights[: _MAX_EVIDENCE + 8],
    }


# -----------------------------------------------------------------------------
# 主入口
# -----------------------------------------------------------------------------
def build_report(
    session: Session,
    *,
    kind: str = "monthly",
    start: date | None = None,
    end: date | None = None,
    today: date | None = None,
    include: set[str] | None = None,
) -> dict[str, Any]:
    """构建一份报告。

    ``include`` 可以选择只要其中几节（界面上的"自定义报告"用得上）。
    """
    start, end = resolve_period(kind, start=start, end=end, today=today)
    totals = _period_totals(session, start, end)

    builders: dict[str, Any] = {
        "overview": lambda: _section_overview(session, start, end, kind, totals),
        "breakdown": lambda: _section_breakdown(session, start, end),
        "accounts": lambda: _section_accounts(session, start, end),
        "budgets": lambda: _section_budgets(session, end),
        "piggy": lambda: _section_piggy(session),
        "trend": lambda: _section_kline(session, start, end),
        "calendar": lambda: _section_calendar(session, start, end),
        "payroll": lambda: _section_payroll(session, start, end),
        "insights": lambda: _section_insights(session, start, end, totals),
    }
    selected = [key for key in builders if include is None or key in include]
    sections = [builders[key]() for key in selected]

    insights = next((item.get("insights", []) for item in sections if item["key"] == "insights"), [])
    highlights = [item["title"] for item in insights if item["level"] in {"critical", "warn", "good"}][:3]
    headline = _headline(kind, start, end, totals)
    label = _label(kind, start, end)

    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "title": f"{label}报告",
        "subtitle": headline,
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": label,
            "days": (end - start).days + 1,
        },
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "currency": DEFAULT_CURRENCY,
        "cover": {"headline": headline, "highlights": highlights},
        "kpis": _top_kpis(totals, sections),
        "sections": sections,
        "insights": insights,
        "notes": [
            "金额均为最小单位（分）。转账与余额调整不计入收支统计。",
            "本报告完全离线生成，未联网、未调用任何模型。",
        ],
    }


def _headline(kind: str, start: date, end: date, totals: dict[str, int]) -> str:
    """封面的一句话。零数据时也要给出**可读的**一句话，而不是空字符串。"""
    if totals["income_minor"] == 0 and totals["expense_minor"] == 0:
        return f"{_label(kind, start, end)}没有收支记录"
    net = totals["net_minor"]
    direction = "结余" if net >= 0 else "净流出"
    return (
        f"{_label(kind, start, end)}：收入 {_yuan(totals['income_minor'])}、"
        f"支出 {_yuan(totals['expense_minor'])}，{direction} {_yuan(abs(net))}"
    )


def _yuan(minor: int) -> str:
    """最小单位 → 元，用于封面文案。

    刻意在**后端**把封面文案拼成一句人话：导出 Markdown 时没必要让它
    再实现一遍"元/分换算 + 正负号"。
    """
    return f"{minor / 100:.2f}"


def _top_kpis(totals: dict[str, int], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kpis = [
        _money_metric("income", "收入", totals["income_minor"], tone="positive"),
        _money_metric("expense", "支出", totals["expense_minor"], tone="negative"),
        _money_metric(
            "net",
            "净额",
            totals["net_minor"],
            tone="positive" if totals["net_minor"] >= 0 else "negative",
        ),
    ]
    # 净值取趋势节里的期末值（若有）
    for section in sections:
        for block in section.get("blocks", []):
            if block["type"] == "metrics":
                for item in block["items"]:
                    if item["key"] == "closing_net_worth":
                        kpis.append({**item, "label": "期末净值"})
    return kpis


def section_keys() -> list[str]:
    """可选的节名。界面用它来让用户挑要哪些节。"""
    return [
        "overview",
        "breakdown",
        "accounts",
        "budgets",
        "piggy",
        "trend",
        "calendar",
        "payroll",
        "insights",
    ]
