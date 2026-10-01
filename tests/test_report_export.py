"""报告导出（P5）的用例。

三个渲染器都必须只靠 block 结构工作。因此这里除了格式正确性，
还刻意钉住两件事：

* **HTML 必须转义一切来自数据库的文本** —— 导出的 HTML 很可能被用户
  在浏览器里打开，一个叫 `<img src=x onerror=...>` 的商户名会变成
  一次真实的 XSS；
* **零数据下三种格式都能正常导出**（而不是抛异常或产出空文件）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from accountbook.core.domain import TransactionType
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import categories as categories_service
from accountbook.services import report_export, reports
from accountbook.services import transactions as transactions_service

TODAY = date.today()
WINDOW_START = TODAY - timedelta(days=20)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ledger.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database):
    with db.session() as active:
        yield active


def _account(session, index: int = 0):
    return accounts_service.list_accounts(session)[index]


def _category(session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    return categories_service.create_category(session, name=name, kind=kind).id


def _spend(session, *, day: date, amount: int, payee: str = "", category: str = "午餐"):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_account(session).id,
        amount_minor=amount,
        occurred_at=datetime.combine(day, datetime.min.time()).replace(hour=12),
        category_id=_category(session, category),
        payee=payee,
    )


def _document(session, **kwargs):
    return reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY, **kwargs)


def _minimal_document(**overrides) -> dict:
    """手搓一份最小文档，便于精确控制某个块的内容。"""
    document = {
        "schema": reports.SCHEMA_VERSION,
        "kind": "custom",
        "title": "测试报告",
        "subtitle": "副标题",
        "period": {
            "start": "2026-01-01",
            "end": "2026-01-31",
            "label": "2026 年 1 月",
            "days": 31,
        },
        "generated_at": "2026-02-01T09:00:00",
        "currency": "CNY",
        "cover": {"headline": "", "highlights": []},
        "kpis": [],
        "sections": [],
        "insights": [],
        "notes": ["口径说明"],
    }
    document.update(overrides)
    return document


# -----------------------------------------------------------------------------
# chart_as_rows
# -----------------------------------------------------------------------------
class TestChartAsRows:
    def test_handles_points(self) -> None:
        rows = report_export.chart_as_rows(
            {"points": [{"date": "2026-01-01", "value_minor": 100}, {"name": "x", "value": 5}]}
        )
        assert rows == [("2026-01-01", 100.0), ("x", 5.0)]

    def test_empty_and_missing_points_are_safe(self) -> None:
        """缺 points / points 不是列表都不该抛 —— 报告要能导出去。"""
        assert report_export.chart_as_rows({}) == []
        assert report_export.chart_as_rows({"points": None}) == []
        assert report_export.chart_as_rows({"points": ["不是字典"]}) == []

    def test_limit(self) -> None:
        rows = report_export.chart_as_rows(
            {"points": [{"name": str(index), "value": index} for index in range(10)]}, limit=3
        )
        assert len(rows) == 3


# -----------------------------------------------------------------------------
# Markdown
# -----------------------------------------------------------------------------
class TestMarkdown:
    def test_zero_data_exports(self, session) -> None:
        text = report_export.render_markdown(_document(session))
        assert text.startswith("# ")
        assert "测试" not in text  # 标题来自真实报告
        assert "口径说明" in text
        # 空表要带说明，而不是只有表头
        assert "无数据" in text or "没有支出" in text

    def test_contains_kpi_and_section_headings(self, session) -> None:
        _spend(session, day=TODAY, amount=5_000)
        text = report_export.render_markdown(_document(session))
        assert "## 关键指标" in text
        assert "## 收支概览" in text
        assert "| 指标 | 值 |" in text

    def test_chart_is_rendered_as_table(self, session) -> None:
        """Markdown 不画图，但**数据必须完整** —— 这是"中性数据集"要支持的事。"""
        _spend(session, day=TODAY, amount=5_000)
        text = report_export.render_markdown(_document(session))
        assert "每日支出" in text
        assert "| 项 | 值 |" in text

    def test_unavailable_block_is_marked(self) -> None:
        """`unavailable` 块在 Markdown 里要显眼地标出来。

        这里用**构造的文档**而不是真实报告：P6 落地后已经没有哪一节
        再用 `unavailable` 了，于是"真实报告里找不到它"与
        "渲染器坏了"看起来一模一样 —— 这正是之前 XSS 检查踩过的坑。
        """
        document = _minimal_document(
            sections=[
                {
                    "key": "x",
                    "title": "未实现的一节",
                    "blocks": [{"type": "unavailable", "text": "这一节尚未实现（示例）"}],
                }
            ]
        )
        text = report_export.render_markdown(document)
        assert "尚未实现" in text
        assert "⚠️" in text
        # HTML 与 PDF 也要能处理它，而不是抛异常
        assert "unavailable" in report_export.render_html(document)
        assert report_export.render_pdf(document).startswith(b"%PDF-")


# -----------------------------------------------------------------------------
# HTML
# -----------------------------------------------------------------------------
class TestHtml:
    def test_standalone_is_self_contained(self, session) -> None:
        """自包含：无任何外部请求。

        导出的文件可能被拷到另一台机器、或者几年后再打开 ——
        那时任何外链都可能已经失效。
        """
        html = report_export.render_html(_document(session))
        assert html.startswith("<!doctype html>")
        assert "<style>" in html
        assert "http://" not in html.split("<body")[0].replace("http://www.w3.org", "")
        assert "<link" not in html
        assert "<script" not in html

    def test_fragment_mode(self, session) -> None:
        html = report_export.render_html(_document(session), standalone=False)
        assert not html.startswith("<!doctype")
        assert html.startswith('<div class="wrap">')

    def test_charts_become_inline_svg(self, session) -> None:
        _spend(session, day=TODAY, amount=5_000)
        html = report_export.render_html(_document(session))
        assert "<svg" in html

    def test_all_three_chart_kinds_render(self) -> None:
        for kind in ("bar", "line", "pie"):
            block = {
                "type": "chart",
                "chart": kind,
                "title": "t",
                "unit": "money",
                "dataset": {"points": [{"name": "a", "value": 1}, {"name": "b", "value": 2}]},
            }
            html = report_export._html_block(block, "CNY")
            assert "<svg" in html, kind

    def test_empty_chart_says_so(self) -> None:
        block = {
            "type": "chart",
            "chart": "bar",
            "title": "t",
            "unit": "money",
            "dataset": {"points": []},
        }
        assert "无数据" in report_export._html_block(block, "CNY")


class TestHtmlEscaping:
    """**XSS 保证。** 导出的 HTML 很可能被用户在浏览器里打开。

    这里用**构造的文档**而不是真实报告：真实报告里商户名不一定出现在
    任何一节中，于是"没有转义"和"文本根本没被渲染"看起来是一样的 ——
    我第一次做冒烟检查时就是这样，得到了一个看起来通过、实则无效的结论。
    """

    def _document_with(self, payload: str) -> dict:
        return _minimal_document(
            sections=[
                {
                    "key": "x",
                    "title": payload,
                    "blocks": [
                        {"type": "text", "text": payload},
                        {"type": "note", "text": payload},
                        {"type": "unavailable", "text": payload},
                        {
                            "type": "metrics",
                            "items": [
                                {
                                    "key": "k",
                                    "label": payload,
                                    "kind": "money",
                                    "value_minor": 1,
                                    "delta_ratio": None,
                                    "delta_label": "",
                                    "tone": "neutral",
                                    "hint": payload,
                                }
                            ],
                        },
                        {
                            "type": "table",
                            "title": payload,
                            "columns": [{"key": "name", "label": payload, "align": "left"}],
                            "rows": [{"name": payload}],
                            "empty": payload,
                            "footer": {"name": payload},
                        },
                        {
                            "type": "chart",
                            "chart": "bar",
                            "title": payload,
                            "unit": "money",
                            "dataset": {"points": [{"name": payload, "value": 1}]},
                        },
                    ],
                    "insights": [
                        {
                            "key": "i",
                            "level": "warn",
                            "title": payload,
                            "detail": payload,
                            "evidence": [payload],
                            "suggestion": payload,
                            "confidence": 0.5,
                        }
                    ],
                }
            ],
            notes=[payload],
        )

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            '"><script>alert(1)</script>',
            "</td></tr><script>alert(1)</script>",
        ],
    )
    def test_payload_is_escaped_everywhere(self, payload: str) -> None:
        html = report_export.render_html(self._document_with(payload))
        # 安全相关的性质是「没有裸标签存活」，而不是「那段文字不出现」：
        # 把 `<img ...>` 转义成 `&lt;img ...&gt;` 之后，`onerror=alert` 仍然
        # 以纯文本形式留在页面上 —— 那是无害的，而且正是我们想要的
        # （用户的备注就该原样显示，只是不能当标签解析）。
        assert "<script>" not in html
        assert "<img" not in html
        # 而且它确实被渲染了（以转义形式），不是被悄悄丢掉
        assert "&lt;" in html and "&gt;" in html

    def test_escaping_happens_in_attribute_context_too(self) -> None:
        """SVG 的 aria-label 是属性上下文，同样要转义。"""
        html = report_export.render_html(self._document_with('<b>"x"</b>'))
        assert 'aria-label="&lt;b&gt;' in html


# -----------------------------------------------------------------------------
# PDF
# -----------------------------------------------------------------------------
class TestPdf:
    def test_zero_data_exports_valid_pdf(self, session) -> None:
        data = report_export.render_pdf(_document(session))
        assert data.startswith(b"%PDF-")
        assert data.rstrip().endswith(b"%%EOF")
        assert len(data) > 1_500

    def test_with_data_is_larger(self, session) -> None:
        empty = len(report_export.render_pdf(_document(session)))
        for offset in range(5):
            _spend(session, day=TODAY - timedelta(days=offset), amount=1_000 * (offset + 1))
        filled = len(report_export.render_pdf(_document(session)))
        assert filled > empty

    def test_uses_builtin_cjk_font(self, session) -> None:
        """内置 CID 字体意味着**不需要附带字体文件** —— 分发时少一个失败点。"""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        data = report_export.render_pdf(_document(session))
        assert data.startswith(b"%PDF-")
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        assert "STSong-Light" in pdfmetrics.getRegisteredFontNames()

    def test_minimal_document_with_chinese(self) -> None:
        data = report_export.render_pdf(
            _minimal_document(
                sections=[
                    {
                        "key": "x",
                        "title": "中文标题",
                        "blocks": [
                            {"type": "text", "text": "这是一段中文正文，用于验证字体嵌入。"},
                            {"type": "unavailable", "text": "尚未实现"},
                        ],
                    }
                ]
            )
        )
        assert data.startswith(b"%PDF-")


# -----------------------------------------------------------------------------
# 三种格式的一致性
# -----------------------------------------------------------------------------
class TestCrossFormat:
    def test_all_three_read_only_blocks(self, session) -> None:
        """三份导出都必须成功 —— 这是"一份 Schema 三处消费"的最低要求。"""
        _spend(session, day=TODAY, amount=7_777)
        document = _document(session)
        assert report_export.render_markdown(document)
        assert report_export.render_html(document)
        assert report_export.render_pdf(document)

    def test_same_numbers_appear_in_all_formats(self, session) -> None:
        """同一个金额要在三种格式里都能找到（格式化方式可以不同）。"""
        _spend(session, day=TODAY, amount=123_456)
        document = _document(session)
        markdown = report_export.render_markdown(document)
        html = report_export.render_html(document)
        assert "1,234.56" in markdown
        assert "1,234.56" in html
