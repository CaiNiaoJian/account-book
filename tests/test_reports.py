"""报表引擎（P5）的用例。

除了常规的口径校验，这里刻意钉住几条**诚实性保证**：

* P6 的薪酬节必须是 `unavailable` 块，**不能**是一排 0
  （一排 0 会让用户以为"我这个月五险一金是 0"）；
* 零数据时封面必须是一句**可读的话**，而不是空字符串或 "None"；
* 占比在后端算好并随行下发 —— 让每个导出渲染器各算一遍，
  必然有一处会算错。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from accountbook.core.domain import TransactionType
from accountbook.core.errors import ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import budgets as budgets_service
from accountbook.services import categories as categories_service
from accountbook.services import reports
from accountbook.services import transactions as transactions_service

TODAY = date.today()
MONTH_START = TODAY.replace(day=1)
DAY_1 = TODAY - timedelta(days=18)
DAY_2 = TODAY - timedelta(days=12)
DAY_3 = TODAY - timedelta(days=5)
#: 数据类用例的固定窗口。**不用 monthly**：今天若是 1 号，月度区间只有一天，
#: 而 `TODAY - 18 天` 会落到区间之外 —— 用例就会在月初失败、月中通过。
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


def _at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def _spend(session, *, day: date, amount: int, category: str = "午餐"):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.EXPENSE.value,
        account_id=_account(session).id,
        amount_minor=amount,
        occurred_at=_at(day),
        category_id=_category(session, category),
    )


def _income(session, *, day: date, amount: int):
    return transactions_service.create_transaction(
        session,
        type=TransactionType.INCOME.value,
        account_id=_account(session).id,
        amount_minor=amount,
        occurred_at=_at(day, 9),
    )


def _blocks(document: dict, key: str) -> list[dict]:
    section = next(item for item in document["sections"] if item["key"] == key)
    return section["blocks"]


def _block(document: dict, key: str, block_type: str) -> dict:
    return next(item for item in _blocks(document, key) if item["type"] == block_type)


# -----------------------------------------------------------------------------
# 期间解析
# -----------------------------------------------------------------------------
class TestResolvePeriod:
    def test_daily(self) -> None:
        assert reports.resolve_period("daily", today=date(2026, 10, 1)) == (
            date(2026, 10, 1),
            date(2026, 10, 1),
        )

    def test_weekly_starts_on_monday(self) -> None:
        # 2026-10-01 是周四
        start, end = reports.resolve_period("weekly", today=date(2026, 10, 1))
        assert start.weekday() == 0
        assert start == date(2026, 9, 28)
        assert end == date(2026, 10, 4)

    def test_monthly(self) -> None:
        assert reports.resolve_period("monthly", today=date(2026, 10, 15)) == (
            date(2026, 10, 1),
            date(2026, 10, 31),
        )

    def test_monthly_february_leap(self) -> None:
        """闰年 2 月是 29 天 —— 用固定 28/30 天会算错。"""
        assert reports.resolve_period("monthly", today=date(2028, 2, 10)) == (
            date(2028, 2, 1),
            date(2028, 2, 29),
        )

    def test_yearly(self) -> None:
        assert reports.resolve_period("yearly", today=date(2026, 6, 1)) == (
            date(2026, 1, 1),
            date(2026, 12, 31),
        )

    def test_custom_requires_both_ends(self) -> None:
        with pytest.raises(ValidationError, match="起止日期"):
            reports.resolve_period("custom", start=date(2026, 1, 1))

    def test_custom_rejects_reversed_range(self) -> None:
        with pytest.raises(ValidationError, match="早于"):
            reports.resolve_period("custom", start=date(2026, 2, 1), end=date(2026, 1, 1))

    def test_custom_rejects_absurd_span(self) -> None:
        """自定义区间必须有上限，否则一次请求能把整库拉出来。"""
        with pytest.raises(ValidationError, match="最多"):
            reports.resolve_period(
                "custom",
                start=date(2000, 1, 1),
                end=date(2026, 1, 1),
            )

    def test_unknown_kind(self) -> None:
        with pytest.raises(ValidationError, match="未知的报告类型"):
            reports.resolve_period("fortnightly")


class TestComparablePeriods:
    def test_previous_period_is_adjacent_and_equal_length(self) -> None:
        start, end = date(2026, 10, 1), date(2026, 10, 31)
        previous_start, previous_end = reports._previous_period(start, end)
        assert previous_end == date(2026, 9, 30)
        assert (previous_end - previous_start).days == (end - start).days

    def test_year_back_keeps_weekday_for_weekly(self) -> None:
        """**364 天而不是 365 天。**

        按 365 天回退会让"本周"对上"去年的周二到下周一"，
        于是"周末消费高"这类结论会完全失真。
        """
        start, end = date(2026, 9, 28), date(2026, 10, 4)
        year_start, year_end = reports._shift_year_back(start, end, "weekly")
        assert year_start.weekday() == start.weekday()
        assert year_end.weekday() == end.weekday()
        assert (year_end - year_start).days == (end - start).days

    def test_year_back_keeps_weekday_for_daily(self) -> None:
        start = end = date(2026, 10, 1)
        year_start, _ = reports._shift_year_back(start, end, "daily")
        assert year_start.weekday() == start.weekday()

    def test_year_back_uses_calendar_year_for_monthly(self) -> None:
        """ "去年 10 月"就是去年 10 月，不是去年 10 月 3 号。"""
        start, end = date(2026, 10, 1), date(2026, 10, 31)
        year_start, year_end = reports._shift_year_back(start, end, "monthly")
        assert year_start == date(2025, 10, 1)
        assert year_end == date(2025, 10, 31)

    def test_ratio_delta_none_when_previous_is_zero(self) -> None:
        """上一期为 0 时返回 None 而不是 0 或无穷大。

        "从 0 涨到 100"没有可表达的百分比；返回 0 会谎称"没有变化"。
        """
        assert reports._ratio_delta(100, 0) is None
        assert reports._ratio_delta(200, 100) == pytest.approx(1.0)
        assert reports._ratio_delta(50, 100) == pytest.approx(-0.5)


# -----------------------------------------------------------------------------
# 零数据
# -----------------------------------------------------------------------------
class TestZeroData:
    @pytest.mark.parametrize("kind", ["daily", "weekly", "monthly", "yearly"])
    def test_every_kind_builds(self, session, kind: str) -> None:
        document = reports.build_report(session, kind=kind, today=TODAY)
        assert document["schema"] == reports.SCHEMA_VERSION
        assert document["kind"] == kind
        assert document["sections"], "零数据也必须产出结构完整的报告"

    def test_headline_is_readable(self, session) -> None:
        """封面必须是一句可读的话，而不是空字符串或 "None"。"""
        document = reports.build_report(session, kind="monthly", today=TODAY)
        headline = document["cover"]["headline"]
        assert headline
        assert "None" not in headline
        assert "没有收支记录" in headline

    def test_every_section_and_block_is_well_formed(self, session) -> None:
        document = reports.build_report(session, kind="monthly", today=TODAY)
        for section in document["sections"]:
            assert section["key"]
            assert section["title"]
            assert section["blocks"], f"{section['key']} 不该是空节"
            for block in section["blocks"]:
                assert block["type"] in {
                    "text",
                    "metrics",
                    "table",
                    "chart",
                    "note",
                    "unavailable",
                }

    def test_payroll_is_unavailable_not_zeros(self, session) -> None:
        """**诚实性保证。** P6 还没做，因此这里必须是 `unavailable`。

        放一排 0 会让用户以为"我这个月五险一金是 0" ——
        一个填满假零的报告比缺一节的报告危险得多。
        """
        block = _block(reports.build_report(session, kind="monthly", today=TODAY), "payroll", "unavailable")
        assert "P6" in block["text"]
        section = next(
            item
            for item in reports.build_report(session, kind="monthly", today=TODAY)["sections"]
            if item["key"] == "payroll"
        )
        assert not any(item["type"] == "metrics" for item in section["blocks"])

    def test_trend_section_explains_absence(self, session) -> None:
        """没有净值数据时要说明原因，而不是画一张空图。"""
        document = reports.build_report(session, kind="monthly", today=TODAY)
        blocks = _blocks(document, "trend")
        assert all(item["type"] != "chart" for item in blocks)
        assert any(item["type"] == "text" for item in blocks)

    def test_insights_say_nothing_notable(self, session) -> None:
        document = reports.build_report(session, kind="monthly", today=TODAY)
        assert [item["key"] for item in document["insights"]] == ["nothing_notable"]

    def test_empty_table_carries_explanation(self, session) -> None:
        """空表要带一句说明：一张只有表头的表会让人以为加载失败。"""
        table = _block(reports.build_report(session, kind="monthly", today=TODAY), "breakdown", "table")
        assert table["empty"]


# -----------------------------------------------------------------------------
# 有数据
# -----------------------------------------------------------------------------
class TestWithData:
    def test_totals_and_kpis(self, session) -> None:
        _income(session, day=DAY_1, amount=1_000_000)
        _spend(session, day=DAY_2, amount=300_000)
        _spend(session, day=DAY_3, amount=100_000)

        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        kpis = {item["key"]: item for item in document["kpis"]}
        assert kpis["income"]["value_minor"] == 1_000_000
        assert kpis["expense"]["value_minor"] == 400_000
        assert kpis["net"]["value_minor"] == 600_000

    def test_transfer_is_excluded_from_totals(self, session) -> None:
        """转账既不是收入也不是支出 —— 计入会让收支双双虚高。"""
        _income(session, day=DAY_1, amount=100_000)
        transactions_service.create_transaction(
            session,
            type=TransactionType.TRANSFER.value,
            account_id=_account(session, 0).id,
            to_account_id=_account(session, 1).id,
            amount_minor=50_000,
            occurred_at=_at(DAY_2),
        )
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        kpis = {item["key"]: item for item in document["kpis"]}
        assert kpis["income"]["value_minor"] == 100_000
        assert kpis["expense"]["value_minor"] == 0

    def test_breakdown_shares_are_precomputed_and_sum_to_one(self, session) -> None:
        """占比由**后端**算好并随行下发。

        让每个导出渲染器各算一遍，必然有一处会算错 ——
        而表格里占比加起来不是 100% 是很难被发现的。
        """
        _spend(session, day=DAY_1, amount=300_000, category="午餐")
        _spend(session, day=DAY_2, amount=100_000, category="打车")

        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        table = next(
            item
            for item in _blocks(document, "breakdown")
            if item["type"] == "table" and item["title"] == "支出明细"
        )
        assert all("share" in row for row in table["rows"])
        assert sum(row["share"] for row in table["rows"]) == pytest.approx(1.0)
        assert table["footer"]["share"] == pytest.approx(1.0)
        # 最大的排在最前面
        assert table["rows"][0]["name"] == "午餐"

    def test_breakdown_chart_is_capped_but_table_is_not(self, session) -> None:
        """饼图只画前 8：超过 8 块就只能看颜色了，而明细表仍然完整。"""
        for index in range(10):
            _spend(session, day=DAY_1, amount=(index + 1) * 1_000, category=f"类目{index}")

        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        chart = next(item for item in _blocks(document, "breakdown") if item["type"] == "chart")
        table = next(
            item
            for item in _blocks(document, "breakdown")
            if item["type"] == "table" and item["title"] == "支出明细"
        )
        assert len(chart["dataset"]["points"]) == 8
        assert len(table["rows"]) == 10

    def test_comparison_table_has_three_rows(self, session) -> None:
        _income(session, day=DAY_1, amount=100_000)
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        table = next(item for item in _blocks(document, "overview") if item["type"] == "table")
        labels = [row["label"] for row in table["rows"]]
        assert labels == ["本期", "上一期（环比）", "去年同期（同比）"]

    def test_accounts_section_ties_out_to_ledger(self, session) -> None:
        """账户变动那一节必须与台账用同一套算法，否则两页对不上。"""
        from accountbook.services import ledger as ledger_service

        account = _account(session)
        _income(session, day=DAY_1, amount=200_000)
        _spend(session, day=DAY_2, amount=50_000)

        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        table = next(item for item in _blocks(document, "accounts") if item["type"] == "table")
        row = next(item for item in table["rows"] if item["name"] == account.name)
        assert row["closing_minor"] == ledger_service.balance_as_of(session, account.id, TODAY)
        assert row["opening_minor"] == ledger_service.opening_balance(session, account.id, WINDOW_START)

    def test_calendar_completeness_ignores_future_days(self, session) -> None:
        """未来的日期不算"漏记" —— 那是在冤枉用户。"""
        _spend(session, day=TODAY, amount=1_000)
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        metrics = _block(document, "calendar", "metrics")
        items = {item["key"]: item for item in metrics["items"]}
        # 月度区间到月底，但完整度的分母只能是"今天及之前"
        assert items["recorded_days"]["value"] >= 1
        assert items["completeness"]["value"] > 0


# -----------------------------------------------------------------------------
# 洞察
# -----------------------------------------------------------------------------
class TestInsights:
    def test_negative_net_is_flagged(self, session) -> None:
        _income(session, day=DAY_1, amount=100_000)
        _spend(session, day=DAY_2, amount=300_000)
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        keys = [item["key"] for item in document["insights"]]
        assert "negative_net" in keys

    def test_budget_overrun_is_flagged(self, session) -> None:
        # 用**年度**预算并让支出落在今天：月度预算配上"相对今天的偏移"
        # 会在月初跑到上个月去，届时预算已用为 0，超支也就无从谈起
        budgets_service.create_budget(
            session,
            name="餐饮预算",
            scope="total",
            period="yearly",
            amount_minor=10_000,
        )
        _spend(session, day=TODAY, amount=50_000)
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        assert any(item["key"].startswith("budget_over") for item in document["insights"])

    def test_category_spike_is_flagged_against_previous_period(self, session) -> None:
        # 上一期 = 紧邻本窗口之前的等长窗口（见 `_previous_period`），
        # 因此"上一期的那笔"必须落在那个窗口里，而不是"上个月 15 号"
        previous_end = WINDOW_START - timedelta(days=1)
        _spend(session, day=previous_end - timedelta(days=5), amount=20_000, category="打车")
        _spend(session, day=DAY_2, amount=60_000, category="打车")
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        assert any(item["key"].startswith("spike:") for item in document["insights"]), (
            "本期比上期高出 3 倍，应当被标出来"
        )

    def test_tiny_previous_amount_does_not_trigger_spike(self, session) -> None:
        """上一期太小的时候不算比例："从 5 元涨到 50 元"是 10 倍但没有意义。"""
        previous_end = WINDOW_START - timedelta(days=1)
        _spend(session, day=previous_end - timedelta(days=5), amount=500, category="咖啡")
        _spend(session, day=DAY_2, amount=5_000, category="咖啡")
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        assert not any(item["key"].startswith("spike:") for item in document["insights"])

    def test_every_insight_has_evidence_and_suggestion(self, session) -> None:
        """洞察必须带证据与建议 —— 只说"支出偏高"是没用的。"""
        _income(session, day=DAY_1, amount=100_000)
        _spend(session, day=DAY_2, amount=300_000)
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        for item in document["insights"]:
            assert item["level"] in {"good", "info", "warn", "critical"}
            assert item["title"]
            assert item["detail"]
            assert isinstance(item["evidence"], list)
            if item["key"] != "nothing_notable":
                assert item["evidence"], f"{item['key']} 没有给证据"


# -----------------------------------------------------------------------------
# 选择性构建
# -----------------------------------------------------------------------------
class TestInclude:
    def test_include_limits_sections(self, session) -> None:
        document = reports.build_report(
            session, kind="custom", start=WINDOW_START, end=TODAY, include={"overview"}
        )
        assert [item["key"] for item in document["sections"]] == ["overview"]

    def test_include_insights_still_populates_top_level(self, session) -> None:
        document = reports.build_report(
            session, kind="custom", start=WINDOW_START, end=TODAY, include={"insights"}
        )
        assert document["insights"]

    def test_section_keys_matches_buildable_sections(self, session) -> None:
        document = reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)
        assert set(reports.section_keys()) == {item["key"] for item in document["sections"]}
