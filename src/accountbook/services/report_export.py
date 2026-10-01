"""报告导出渲染器（P5 / 需求 21）。

这是对"一份 Schema，三处消费"的第一次真正检验
==============================================
三个渲染器**只能读 block 结构**，不许去猜某个字段的业务含义。
如果哪个渲染器不得不写 `if section == 'breakdown'` 这种分支，
那就说明 Schema 少了东西 —— 而那种分支正是"新增一节要改三处"的开端。

每个渲染器都只处理七种块
------------------------
`text | metrics | table | chart | note | unavailable`

图表怎么处理：HTML 画真的，Markdown / PDF 落成表
-------------------------------------------------
HTML 是三者里表现力最强的目标（自包含、可打印、可在浏览器里看），
因此内联 SVG 画柱/折线/饼。
Markdown 与 PDF 把同一个 `dataset` 落成一张表 —— 这正是
"中性数据集"这个设计要支持的事：**导不出图时，数据本身仍然是完整的**。

安全
----
HTML 导出必须转义一切来自数据库的文本（商户、备注、分类名、账户名）。
导出的 HTML 很可能被用户在浏览器里打开，一个叫
`<img src=x onerror=...>` 的商户名会变成一次真实的 XSS。
`html.escape` 全程使用，且**标签是我们自己拼的，数据一律经过转义**。

PDF 的中文字体
--------------
用 reportlab 内置的 CID 字体 `STSong-Light`，**不需要附带字体文件** ——
分发时少一个几 MB 的资源，也少一处"路径找不到"的失败点。
"""

from __future__ import annotations

import html
from io import BytesIO
from typing import Any

from ..core.money import DEFAULT_CURRENCY, currency_symbol, from_minor

__all__ = ["chart_as_rows", "render_html", "render_markdown", "render_pdf"]

#: PDF 里的中文字体名（CID 字体，reportlab 自带映射）
_PDF_FONT = "STSong-Light"
_PDF_FONT_BOLD = "STSong-Light"


# -----------------------------------------------------------------------------
# 共用：数字与文本
# -----------------------------------------------------------------------------
def _money(minor: int, currency: str = DEFAULT_CURRENCY) -> str:
    """最小单位 → ``¥1,234.56``。

    负号放在货币符号**之前**（``-¥12.00``）而不是 ``¥-12.00`` ——
    后者在中文排版里几乎没人那么写，扫读时容易看漏那个符号。
    千分位也在这里加：导出的报告是要对着数字看的。
    """
    value = from_minor(int(minor), currency)
    sign = "-" if value < 0 else ""
    return f"{sign}{currency_symbol(currency)}{abs(value):,.2f}"


def _percent(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _format_value(value: Any, kind: str, currency: str) -> str:
    if value is None or value == "":
        return "—"
    if kind == "money":
        return _money(int(value), currency)
    if kind == "percent":
        return _percent(float(value))
    return str(value)


def _delta(ratio: float | None, label: str) -> str:
    if ratio is None:
        return "—" if not label else f"{label} —"
    sign = "+" if ratio >= 0 else ""
    text = f"{sign}{ratio * 100:.1f}%"
    return f"{label} {text}" if label else text


def chart_as_rows(dataset: dict[str, Any], *, limit: int | None = None) -> list[tuple[str, float]]:
    """把中性图表数据集落成 ``[(标签, 数值)]``。

    这是三个渲染器共用的**唯一**图表转换点。它存在的原因是：
    Markdown 与 PDF 都不画图，而它们需要的是同一份数据。
    各写一遍会让"某个图表导出后数值不对"变成只有那一种格式才有的 bug。
    """
    points = dataset.get("points") or []
    rows: list[tuple[str, float]] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            continue
        label = point.get("date") or point.get("name") or str(index + 1)
        value = point.get("value_minor", point.get("value", 0))
        rows.append((str(label), float(value or 0)))
    if limit is not None:
        rows = rows[:limit]
    return rows


# -----------------------------------------------------------------------------
# Markdown
# -----------------------------------------------------------------------------
def _md_table(block: dict[str, Any], currency: str) -> list[str]:
    columns = block["columns"]
    rows = block["rows"]
    lines: list[str] = []
    if block.get("title"):
        lines.append(f"**{block['title']}**")
        lines.append("")
    if not rows:
        lines.append(f"_{block.get('empty') or '无数据'}_")
        lines.append("")
        return lines
    lines.append("| " + " | ".join(column["label"] for column in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        cells = []
        for column in columns:
            cells.append(_format_value(row.get(column["key"]), column.get("format", ""), currency))
        lines.append("| " + " | ".join(cells) + " |")
    if block.get("footer"):
        footer = block["footer"]
        cells = [
            _format_value(footer.get(column["key"]), column.get("format", ""), currency) for column in columns
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _md_block(block: dict[str, Any], currency: str) -> list[str]:
    kind = block["type"]
    if kind == "text":
        return [block["text"], ""]
    if kind == "note":
        return [f"> {block['text']}", ""]
    if kind == "unavailable":
        return [f"> ⚠️ {block['text']}", ""]
    if kind == "metrics":
        lines = ["| 指标 | 值 | 说明 |", "| --- | --- | --- |"]
        for item in block["items"]:
            value = _format_value(item.get("value_minor", item.get("value")), item.get("kind", ""), currency)
            if item.get("delta_ratio") is not None or item.get("delta_label"):
                value += f"（{_delta(item.get('delta_ratio'), item.get('delta_label', ''))}）"
            lines.append(f"| {item['label']} | {value} | {item.get('hint', '')} |")
        lines.append("")
        return lines
    if kind == "table":
        return _md_table(block, currency)
    if kind == "chart":
        rows = chart_as_rows(block["dataset"])
        lines = []
        if block.get("title"):
            lines.append(f"**{block['title']}**")
            lines.append("")
        if not rows:
            lines.append("_无数据_")
            lines.append("")
            return lines
        lines.append("| 项 | 值 |")
        lines.append("| --- | --- |")
        for label, value in rows:
            lines.append(f"| {label} | {_format_value(value, block.get('unit', ''), currency)} |")
        lines.append("")
        return lines
    return []


def render_markdown(document: dict[str, Any]) -> str:
    """导出 Markdown。图表以表格形式呈现（数据完整，只是不画图）。"""
    currency = document.get("currency", DEFAULT_CURRENCY)
    lines: list[str] = []
    lines.append(f"# {document['title']}")
    lines.append("")
    lines.append(f"*{document['subtitle']}*")
    lines.append("")
    period = document["period"]
    lines.append(f"- 区间：{period['start']} — {period['end']}（{period['days']} 天）")
    lines.append(f"- 生成时间：{document['generated_at']}")
    lines.append(f"- Schema：`{document['schema']}`")
    lines.append("")

    if document.get("kpis"):
        lines.append("## 关键指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        for item in document["kpis"]:
            value = _format_value(item.get("value_minor", item.get("value")), item.get("kind", ""), currency)
            lines.append(f"| {item['label']} | {value} |")
        lines.append("")

    for section in document["sections"]:
        lines.append(f"## {section['title']}")
        lines.append("")
        for block in section["blocks"]:
            lines.extend(_md_block(block, currency))
        # 洞察挂在 insights 节之外单独成段更好读
        if section.get("insights"):
            for level, marker in (
                ("critical", "🔴"),
                ("warn", "🟠"),
                ("info", "🔵"),
                ("good", "🟢"),
            ):
                for item in section["insights"]:
                    if item["level"] != level:
                        continue
                    lines.append(f"- {marker} **{item['title']}** — {item['detail']}")
                    for evidence in item.get("evidence", []):
                        lines.append(f"  - 证据：{evidence}")
                    if item.get("suggestion"):
                        lines.append(f"  - 建议：{item['suggestion']}")
            lines.append("")

    if document.get("notes"):
        lines.append("---")
        lines.append("")
        lines.append("### 口径说明")
        lines.append("")
        for note in document["notes"]:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# HTML（自包含）
# -----------------------------------------------------------------------------
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px;
  font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  background: #fafafa; color: #1c1c1e;
}
.wrap { max-width: 900px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 28px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #e5e5ea; }
h3 { font-size: 15px; margin: 18px 0 8px; }
.sub { color: #6c6c70; margin: 0 0 12px; }
.meta { color: #8e8e93; font-size: 12px; margin-bottom: 20px; }
.card { background: #fff; border: 1px solid #e5e5ea; border-radius: 12px; padding: 16px; margin: 14px 0; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.kpi { background: #fff; border: 1px solid #e5e5ea; border-radius: 10px; padding: 12px; }
.kpi .label { color: #6c6c70; font-size: 12px; }
.kpi .value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
.kpi .hint { color: #8e8e93; font-size: 11px; margin-top: 2px; }
.pos { color: #1a7f37; } .neg { color: #c0392b; }
table { width: 100%; border-collapse: collapse; margin: 8px 0 4px; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 10px; border-bottom: 1px solid #efeff2; text-align: left; }
th { color: #6c6c70; font-weight: 500; font-size: 12px; }
tr:last-child td { border-bottom: none; }
tfoot td { font-weight: 600; border-top: 1px solid #e5e5ea; }
.right { text-align: right; }
.note { color: #6c6c70; font-size: 12.5px; border-left: 3px solid #d1d1d6; padding: 6px 0 6px 10px; margin: 10px 0; }
.unavailable { background: #fff7e6; border: 1px solid #ffe0a3; border-radius: 10px; padding: 12px; color: #7a5b00; font-size: 13px; }
.empty { color: #8e8e93; font-style: italic; }
.insight { border-left: 3px solid #d1d1d6; padding: 8px 0 8px 12px; margin: 8px 0; }
.insight.critical { border-color: #c0392b; } .insight.warn { border-color: #e08a00; }
.insight.info { border-color: #0a84ff; } .insight.good { border-color: #1a7f37; }
.insight .t { font-weight: 600; }
.insight ul { margin: 4px 0 0 18px; padding: 0; color: #6c6c70; font-size: 12.5px; }
svg text { font-family: inherit; }
@media print {
  body { background: #fff; padding: 0; }
  .card, .kpi { break-inside: avoid; }
  h2 { break-after: avoid; }
}
@media (prefers-color-scheme: dark) {
  body { background: #1c1c1e; color: #f2f2f7; }
  .card, .kpi { background: #2c2c2e; border-color: #3a3a3c; }
  h2, th, td, tfoot td { border-color: #3a3a3c; }
  .sub, .note, .meta, .kpi .label { color: #aeaeb2; }
  .unavailable { background: #3a2f10; border-color: #6b5310; color: #ffd479; }
}
"""

_INSIGHT_MARK = {"critical": "🔴", "warn": "🟠", "info": "🔵", "good": "🟢"}


def _svg_chart(block: dict[str, Any], currency: str) -> str:
    """把中性数据集画成内联 SVG。只用 dataset，不认识业务字段。"""
    rows = chart_as_rows(block["dataset"], limit=14)
    if not rows:
        return '<p class="empty">无数据</p>'
    unit = block.get("unit", "money")
    values = [value for _, value in rows]
    labels = [label for label, _ in rows]
    width, height = 720, 200
    pad_left, pad_bottom, pad_top = 8, 34, 12
    chart_kind = block.get("chart", "bar")

    if chart_kind == "pie":
        total = sum(abs(value) for value in values) or 1
        # 饼图用 SVG 的 path 弧线；超过 8 块就只剩颜色可辨，因此上层已截断
        cx, cy, radius = 110, height / 2, 72
        start = -90.0
        parts = []
        palette = ["#0a84ff", "#1a7f37", "#e08a00", "#c0392b", "#5e5ce6", "#00b8d4", "#ff375f", "#8e8e93"]
        for index, value in enumerate(values):
            sweep = 360.0 * (abs(value) / total)
            end = start + sweep
            large = 1 if sweep > 180 else 0
            x1 = cx + radius * __import__("math").cos(__import__("math").radians(start))
            y1 = cy + radius * __import__("math").sin(__import__("math").radians(start))
            x2 = cx + radius * __import__("math").cos(__import__("math").radians(end))
            y2 = cy + radius * __import__("math").sin(__import__("math").radians(end))
            parts.append(
                f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{radius},{radius} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
                f'fill="{palette[index % len(palette)]}" />'
            )
            start = end
        legend = "".join(
            f'<g><rect x="230" y="{28 + index * 18}" width="10" height="10" '
            f'fill="{palette[index % len(palette)]}" />'
            f'<text x="248" y="{37 + index * 18}" font-size="12">{html.escape(labels[index])} · '
            f"{html.escape(_format_value(values[index], unit, currency))}</text></g>"
            for index in range(len(values))
        )
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="{html.escape(block.get("title", "图表"))}">'
            f"{''.join(parts)}{legend}</svg>"
        )

    if chart_kind == "line":
        hi = max(values) if values else 1
        lo = min(0, min(values) if values else 0)
        span = (hi - lo) or 1
        step = (width - pad_left * 2) / max(1, len(values) - 1)
        points = " ".join(
            f"{pad_left + index * step:.1f},"
            f"{pad_top + (height - pad_top - pad_bottom) * (1 - (value - lo) / span):.1f}"
            for index, value in enumerate(values)
        )
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="{html.escape(block.get("title", "图表"))}">'
            f'<polyline fill="none" stroke="#0a84ff" stroke-width="2" points="{points}" />'
            f'<text x="{pad_left}" y="{height - 12}" font-size="11" fill="#8e8e93">'
            f"{html.escape(labels[0])}</text>"
            f'<text x="{width - pad_left}" y="{height - 12}" font-size="11" fill="#8e8e93" '
            f'text-anchor="end">{html.escape(labels[-1])}</text></svg>'
        )

    # 柱状。**必须支持负值**：应发实发瀑布里的减项就是负的，
    # 早先按 `max(0, value)` 算高度，结果所有减项都画成 0 高度、
    # 一根柱子都看不见，而导出的 HTML 看起来像"图表加载失败"。
    positive = [value for value in values if value > 0]
    negative = [value for value in values if value < 0]
    hi = max(positive) if positive else 1
    lo = min(negative) if negative else 0
    span = (hi - lo) or 1
    plot = height - pad_top - pad_bottom
    # 零线：正值向上、负值向下，都从这里出发
    zero_y = pad_top + plot * (hi / span)
    slot = (width - pad_left * 2) / max(1, len(values))
    bars = []
    for index, value in enumerate(values):
        magnitude = plot * (abs(value) / span)
        x = pad_left + index * slot + slot * 0.15
        y = zero_y - magnitude if value >= 0 else zero_y
        tone = "#0a84ff" if value >= 0 else "#c0392b"
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{slot * 0.7:.1f}" '
            f'height="{max(1.0, magnitude):.1f}" rx="2" fill="{tone}" />'
        )
    if lo < 0:
        bars.append(
            f'<line x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_left}" y2="{zero_y:.1f}" '
            f'stroke="#8e8e93" stroke-width="1" stroke-dasharray="3 3" />'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{html.escape(block.get("title", "图表"))}">'
        f"{''.join(bars)}"
        f'<text x="{pad_left}" y="{height - 12}" font-size="11" fill="#8e8e93">'
        f"{html.escape(labels[0])}</text>"
        f'<text x="{width - pad_left}" y="{height - 12}" font-size="11" fill="#8e8e93" '
        f'text-anchor="end">{html.escape(labels[-1])}</text></svg>'
    )


def _html_table(block: dict[str, Any], currency: str) -> str:
    columns = block["columns"]
    rows = block["rows"]
    parts: list[str] = []
    if block.get("title"):
        parts.append(f"<h3>{html.escape(block['title'])}</h3>")
    if not rows:
        parts.append(f'<p class="empty">{html.escape(block.get("empty") or "无数据")}</p>')
        return "".join(parts)
    parts.append("<table><thead><tr>")
    for column in columns:
        align = ' class="right"' if column.get("align") == "right" else ""
        parts.append(f"<th{align}>{html.escape(column['label'])}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for column in columns:
            align = ' class="right"' if column.get("align") == "right" else ""
            text = _format_value(row.get(column["key"]), column.get("format", ""), currency)
            parts.append(f"<td{align}>{html.escape(text)}</td>")
        parts.append("</tr>")
    parts.append("</tbody>")
    if block.get("footer"):
        parts.append("<tfoot><tr>")
        for column in columns:
            align = ' class="right"' if column.get("align") == "right" else ""
            text = _format_value(block["footer"].get(column["key"]), column.get("format", ""), currency)
            parts.append(f"<td{align}>{html.escape(text)}</td>")
        parts.append("</tr></tfoot>")
    parts.append("</table>")
    return "".join(parts)


def _html_block(block: dict[str, Any], currency: str) -> str:
    kind = block["type"]
    if kind == "text":
        return f"<p>{html.escape(block['text'])}</p>"
    if kind == "note":
        return f'<div class="note">{html.escape(block["text"])}</div>'
    if kind == "unavailable":
        return f'<div class="unavailable">⚠️ {html.escape(block["text"])}</div>'
    if kind == "metrics":
        cells = []
        for item in block["items"]:
            value = _format_value(item.get("value_minor", item.get("value")), item.get("kind", ""), currency)
            tone = ""
            if item.get("delta_ratio") is not None:
                tone = "pos" if float(item["delta_ratio"]) >= 0 else "neg"
            delta = (
                f'<div class="hint">{html.escape(_delta(item.get("delta_ratio"), item.get("delta_label", "")))}'
                f"{' · ' + html.escape(item['hint']) if item.get('hint') else ''}</div>"
                if (item.get("delta_ratio") is not None or item.get("hint"))
                else ""
            )
            cells.append(
                f'<div class="kpi"><div class="label">{html.escape(item["label"])}</div>'
                f'<div class="value {tone}">{html.escape(value)}</div>{delta}</div>'
            )
        return f'<div class="kpis">{"".join(cells)}</div>'
    if kind == "table":
        return _html_table(block, currency)
    if kind == "chart":
        title = f"<h3>{html.escape(block['title'])}</h3>" if block.get("title") else ""
        return f'<div class="card">{title}{_svg_chart(block, currency)}</div>'
    return ""


def render_html(document: dict[str, Any], *, standalone: bool = True) -> str:
    """导出**自包含** HTML：样式内联、图表内联 SVG、无任何外部请求。

    「自包含」是硬要求：导出的文件可能被拷到另一台机器、发给自己、
    或者几年后再打开 —— 那时任何外链（CDN 样式、图片）都可能已经失效。
    """
    currency = document.get("currency", DEFAULT_CURRENCY)
    period = document["period"]
    body: list[str] = []
    body.append(f"<h1>{html.escape(document['title'])}</h1>")
    body.append(f'<p class="sub">{html.escape(document["subtitle"])}</p>')
    body.append(
        f'<p class="meta">区间 {period["start"]} — {period["end"]}（{period["days"]} 天） · '
        f"生成于 {html.escape(document['generated_at'])} · schema {html.escape(document['schema'])}</p>"
    )
    if document.get("kpis"):
        body.append('<div class="kpis">')
        for item in document["kpis"]:
            value = _format_value(item.get("value_minor", item.get("value")), item.get("kind", ""), currency)
            body.append(
                f'<div class="kpi"><div class="label">{html.escape(item["label"])}</div>'
                f'<div class="value">{html.escape(value)}</div></div>'
            )
        body.append("</div>")

    for section in document["sections"]:
        body.append(f"<h2>{html.escape(section['title'])}</h2>")
        for block in section["blocks"]:
            body.append(_html_block(block, currency))
        for item in section.get("insights", []):
            evidence = "".join(f"<li>{html.escape(text)}</li>" for text in item.get("evidence", []))
            suggestion = (
                f'<div class="hint">建议：{html.escape(item["suggestion"])}</div>'
                if item.get("suggestion")
                else ""
            )
            body.append(
                f'<div class="insight {html.escape(item["level"])}">'
                f'<div class="t">{_INSIGHT_MARK.get(item["level"], "")} {html.escape(item["title"])}</div>'
                f"<div>{html.escape(item['detail'])}</div>"
                f"{f'<ul>{evidence}</ul>' if evidence else ''}{suggestion}</div>"
            )

    if document.get("notes"):
        body.append("<h2>口径说明</h2>")
        body.append("".join(f'<div class="note">{html.escape(note)}</div>' for note in document["notes"]))

    inner = f'<div class="wrap">{"".join(body)}</div>'
    if not standalone:
        return inner
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(document['title'])}</title>"
        f"<style>{_CSS}</style></head><body>{inner}</body></html>\n"
    )


# -----------------------------------------------------------------------------
# PDF
# -----------------------------------------------------------------------------
def render_pdf(document: dict[str, Any]) -> bytes:
    """导出 PDF（reportlab + 内置 CID 中文字体）。

    图表以**表格**呈现（与 Markdown 一致）。理由：PDF 是留档格式，
    表格里的数字可以复算、可以复制，而一张画得好看的柱状图不行。
    HTML 已经是"给人看"的目标，PDF 承担"能存档、能打印"的职责。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    pdfmetrics.registerFont(UnicodeCIDFont(_PDF_FONT))

    currency = document.get("currency", DEFAULT_CURRENCY)
    period = document["period"]

    title_style = ParagraphStyle("t", fontName=_PDF_FONT, fontSize=20, leading=26, spaceAfter=4)
    sub_style = ParagraphStyle(
        "s", fontName=_PDF_FONT, fontSize=11, leading=16, textColor=colors.HexColor("#555555")
    )
    h2_style = ParagraphStyle("h2", fontName=_PDF_FONT, fontSize=14, leading=20, spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("b", fontName=_PDF_FONT, fontSize=10, leading=15)
    note_style = ParagraphStyle(
        "n", fontName=_PDF_FONT, fontSize=9, leading=13, textColor=colors.HexColor("#666666"), leftIndent=8
    )
    warn_style = ParagraphStyle(
        "w", fontName=_PDF_FONT, fontSize=9.5, leading=14, textColor=colors.HexColor("#7a5b00")
    )

    story: list[Any] = []
    story.append(Paragraph(html.escape(document["title"]), title_style))
    story.append(Paragraph(html.escape(document["subtitle"]), sub_style))
    story.append(
        Paragraph(
            f"区间 {period['start']} — {period['end']}（{period['days']} 天） · "
            f"生成于 {html.escape(document['generated_at'])}",
            note_style,
        )
    )
    story.append(Spacer(1, 6 * mm))

    def _table_flowable(block: dict[str, Any]) -> Any:
        columns = block["columns"]
        header = [Paragraph(f"<b>{html.escape(column['label'])}</b>", body_style) for column in columns]
        data: list[list[Any]] = [header]
        for row in block["rows"]:
            data.append(
                [
                    Paragraph(
                        html.escape(
                            _format_value(row.get(column["key"]), column.get("format", ""), currency)
                        ),
                        body_style,
                    )
                    for column in columns
                ]
            )
        if block.get("footer"):
            data.append(
                [
                    Paragraph(
                        f"<b>{html.escape(_format_value(block['footer'].get(column['key']), column.get('format', ''), currency))}</b>",
                        body_style,
                    )
                    for column in columns
                ]
            )
        table = Table(data, hAlign="LEFT", repeatRows=1)
        style = [
            ("FONTNAME", (0, 0), (-1, -1), _PDF_FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e5e5ea")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if block.get("footer"):
            style.append(("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor("#cccccc")))
        table.setStyle(TableStyle(style))
        return table

    def _chart_flowable(block: dict[str, Any]) -> Any:
        rows = chart_as_rows(block["dataset"], limit=20)
        if not rows:
            return Paragraph("无数据", note_style)
        data = [[Paragraph("<b>项</b>", body_style), Paragraph("<b>值</b>", body_style)]]
        for label, value in rows:
            data.append(
                [
                    Paragraph(html.escape(label), body_style),
                    Paragraph(html.escape(_format_value(value, block.get("unit", ""), currency)), body_style),
                ]
            )
        table = Table(data, hAlign="LEFT", repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), _PDF_FONT),
                    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e5e5ea")),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        return table

    for section in document["sections"]:
        story.append(Paragraph(html.escape(section["title"]), h2_style))
        for block in section["blocks"]:
            kind = block["type"]
            if kind == "text":
                story.append(Paragraph(html.escape(block["text"]), body_style))
            elif kind == "note":
                story.append(Paragraph(html.escape(block["text"]), note_style))
            elif kind == "unavailable":
                story.append(Paragraph("⚠️ " + html.escape(block["text"]), warn_style))
            elif kind == "metrics":
                rows = []
                for item in block["items"]:
                    value = _format_value(
                        item.get("value_minor", item.get("value")), item.get("kind", ""), currency
                    )
                    delta = _delta(item.get("delta_ratio"), item.get("delta_label", ""))
                    rows.append(
                        [
                            Paragraph(html.escape(item["label"]), body_style),
                            Paragraph(html.escape(value), body_style),
                            Paragraph(html.escape(f"{delta} {item.get('hint', '')}".strip()), note_style),
                        ]
                    )
                table = Table(rows, hAlign="LEFT", colWidths=[35 * mm, 35 * mm, 70 * mm])
                table.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), _PDF_FONT),
                            ("TOPPADDING", (0, 0), (-1, -1), 2),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                        ]
                    )
                )
                story.append(table)
            elif kind == "table":
                if block.get("title"):
                    story.append(Paragraph(html.escape(block["title"]), body_style))
                if block["rows"]:
                    story.append(_table_flowable(block))
                else:
                    story.append(Paragraph(html.escape(block.get("empty") or "无数据"), note_style))
            elif kind == "chart":
                if block.get("title"):
                    story.append(Paragraph(html.escape(block["title"]), body_style))
                story.append(_chart_flowable(block))
            story.append(Spacer(1, 2 * mm))
        for item in section.get("insights", []):
            mark = _INSIGHT_MARK.get(item["level"], "")
            story.append(
                Paragraph(
                    f"<b>{mark} {html.escape(item['title'])}</b> — {html.escape(item['detail'])}",
                    body_style,
                )
            )
            for evidence in item.get("evidence", []):
                story.append(Paragraph(f"· {html.escape(evidence)}", note_style))
            if item.get("suggestion"):
                story.append(Paragraph(f"建议：{html.escape(item['suggestion'])}", note_style))
        story.append(Spacer(1, 3 * mm))

    if document.get("notes"):
        story.append(PageBreak())
        story.append(Paragraph("口径说明", h2_style))
        for note in document["notes"]:
            story.append(Paragraph("· " + html.escape(note), note_style))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=document["title"],
        author="记账本",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )

    def _footer(canvas: Any, _doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(_PDF_FONT, 8)
        canvas.setFillColor(colors.HexColor("#8e8e93"))
        canvas.drawString(18 * mm, 10 * mm, document["title"])
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
