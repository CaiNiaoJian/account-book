"""工作日与发薪日（P6）的用例。

这个文件的重心只有一句：**未知就是未知，不能伪装成工作日。**
中国法定节假日每年由国务院另行发布，代码里写死的表必然过期，
而过期是静默的。因此这里断言的不是"某天是不是假期"（我不该知道），
而是"**系统会不会承认自己不知道**"。

另外几组断言针对真实存在的业务情形：
周末与节假日**用不同策略**（很多公司就是"周末顺延、长假提前"）；
1 日遇假期提前可能落到**上一个月**，但归属期间仍留在本月。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from accountbook.core.errors import ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import workdays

# 2026 年 1 月的星期：1 日周四、2 日周五、3 日周六、4 日周日、5 日周一
JAN_1 = date(2026, 1, 1)
JAN_2 = date(2026, 1, 2)
JAN_3 = date(2026, 1, 3)
JAN_4 = date(2026, 1, 4)
JAN_5 = date(2026, 1, 5)
JAN_15 = date(2026, 1, 15)


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


class _Rule:
    """最小规则替身。用 dataclass 而不是真实模型，是为了让用例只关心被算的字段。"""

    def __init__(self, **kwargs):
        self.id = 1
        self.source_id = 1
        self.enabled = True
        self.day_kind = "fixed"
        self.day_of_month = 15
        self.nth = 1
        self.weekend_policy = "advance"
        self.holiday_policy = "advance"
        for key, value in kwargs.items():
            setattr(self, key, value)


def _weekday_anchor(day: date) -> date:
    """把日期挪到最近的一个工作日，作为"未录入节假日"场景下的稳定锚点。"""
    cursor = day
    while workdays.is_weekend(cursor):
        cursor += timedelta(days=1)
    return cursor


# -----------------------------------------------------------------------------
# 单日判定
# -----------------------------------------------------------------------------
class TestWorkdayFact:
    def test_weekend_is_confidently_not_a_workday(self, session) -> None:
        """周末与年份无关，因此**可以确定**地判断。"""
        fact = workdays.workday_fact(session, JAN_3)
        assert fact.is_workday is False
        assert fact.basis == "weekend"
        assert fact.confident is True

    def test_weekday_without_calendar_data_is_only_assumed(self, session) -> None:
        """**这是本文件最重要的断言。**

        2026 年的法定节假日还没录入时，一个周四很可能就是工作日 ——
        但它也可能是春节。系统必须承认自己不知道，
        而不是把未知当成确定。
        """
        fact = workdays.workday_fact(session, JAN_1)
        assert fact.is_workday is True
        assert fact.basis == "assumed"
        assert fact.confident is False
        assert fact.assumed is True

    def test_override_makes_it_confident(self, session) -> None:
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        fact = workdays.workday_fact(session, JAN_1)
        assert fact.is_workday is False
        assert fact.basis == "override"
        assert fact.confident is True
        assert fact.name == "元旦"
        assert fact.kind == "holiday"

    def test_makeup_workday_turns_a_weekend_into_a_workday(self, session) -> None:
        """调休上班：周日本该休息，但那天要上班。"""
        workdays.upsert_override(session, JAN_4, is_workday=True, name="调休")
        fact = workdays.workday_fact(session, JAN_4)
        assert fact.is_workday is True
        assert fact.kind == "makeup_workday"

    def test_any_record_makes_the_whole_year_confident(self, session) -> None:
        """判据是"这一年有没有任何记录"，而不是"有没有 365 条"。

        本表只存例外，因此一条记录就说明"这一年的安排已经录进来了"。
        """
        assert 2026 not in workdays.coverage_years(session)
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        assert 2026 in workdays.coverage_years(session)
        # 同一年里另一个没有任何记录的工作日，现在可以确定地判断
        fact = workdays.workday_fact(session, JAN_5)
        assert fact.confident is True
        assert fact.basis == "weekday"

    def test_is_holiday_returns_the_name(self, session) -> None:
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        assert workdays.is_holiday(session, JAN_1) == "元旦"
        # 调休上班的日子不是假期
        workdays.upsert_override(session, JAN_4, is_workday=True, name="调休")
        assert workdays.is_holiday(session, JAN_4) == ""
        assert workdays.is_holiday(session, JAN_2) == ""

    def test_one_day_one_answer(self, session) -> None:
        """同一天只能有一个说法，否则"这天到底上不上班"就没有答案。"""
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        workdays.upsert_override(session, JAN_1, is_workday=True, name="改主意了")
        rows = workdays.list_overrides(session, year=2026)
        assert len(rows) == 1
        assert rows[0].is_workday is True

    def test_delete_override_restores_the_default(self, session) -> None:
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        workdays.delete_override(session, JAN_1)
        assert workdays.workday_fact(session, JAN_1).basis == "assumed"


# -----------------------------------------------------------------------------
# 日期挪动
# -----------------------------------------------------------------------------
class TestShift:
    def test_advance_moves_backwards(self, session) -> None:
        """遇周末提前到之前最近的工作日。"""
        assert workdays.shift_to_workday(session, JAN_3, "advance") == (JAN_2, -1)

    def test_postpone_moves_forwards(self, session) -> None:
        assert workdays.shift_to_workday(session, JAN_3, "postpone") == (JAN_5, 2)

    def test_none_keeps_the_date(self, session) -> None:
        assert workdays.shift_to_workday(session, JAN_3, "none") == (JAN_3, 0)

    def test_already_a_workday_is_untouched(self, session) -> None:
        assert workdays.shift_to_workday(session, JAN_2, "advance") == (JAN_2, 0)

    def test_shift_respects_makeup_workdays(self, session) -> None:
        """调休上班时，周末**不必**被挪走。"""
        workdays.upsert_override(session, JAN_4, is_workday=True, name="调休")
        assert workdays.shift_to_workday(session, JAN_4, "advance") == (JAN_4, 0)

    def test_unknown_policy_is_rejected(self, session) -> None:
        with pytest.raises(ValidationError, match="未知的调整策略"):
            workdays.shift_to_workday(session, JAN_3, "whatever")

    def test_absurd_calendar_raises_instead_of_looping(self, session) -> None:
        """整年都标成假期时给明确错误，而不是死循环。"""
        cursor = date(2026, 1, 1)
        while cursor.year == 2026:
            workdays.upsert_override(session, cursor, is_workday=False, name="全放假")
            cursor += timedelta(days=1)
        with pytest.raises(ValidationError, match="找不到工作日"):
            workdays.shift_to_workday(session, date(2026, 6, 15), "postpone")

    def test_nth_workday_of_month(self, session) -> None:
        # 2026-01-01 是周四，是当月第 1 个工作日
        assert workdays.nth_workday_of_month(session, 2026, 1, 1) == JAN_1
        assert workdays.nth_workday_of_month(session, 2026, 1, 2) == JAN_2
        # 第 3 个跳过周末 → 1 月 5 日（周一）
        assert workdays.nth_workday_of_month(session, 2026, 1, 3) == JAN_5
        assert workdays.nth_workday_of_month(session, 2026, 1, 0) is None
        assert workdays.nth_workday_of_month(session, 2026, 1, 99) is None


# -----------------------------------------------------------------------------
# 发薪日
# -----------------------------------------------------------------------------
class TestResolvePayday:
    def test_fixed_day_on_a_workday_is_untouched(self, session) -> None:
        rule = _Rule(day_of_month=2)
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.pay_date == JAN_2
        assert result.adjusted is False
        assert result.period == "2026-01"

    def test_weekend_uses_the_weekend_policy(self, session) -> None:
        rule = _Rule(day_of_month=3, weekend_policy="postpone")
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.pay_date == JAN_5
        assert result.reason == "weekend"
        assert result.policy == "postpone"
        assert result.shift_days == 2
        assert result.adjusted is True

    def test_holiday_uses_its_own_policy(self, session) -> None:
        """**周末与节假日用不同策略。**

        很多公司就是"周末顺延、长假提前"。用一套策略会在这种组合上
        直接给错日期 —— 而发薪日错一天对用户是真金白银的事。
        """
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        rule = _Rule(day_of_month=1, weekend_policy="postpone", holiday_policy="advance")
        result = workdays.resolve_payday(session, rule, 2026, 1)
        # 1 日是周四（不是周末），因此只能走 holiday_policy
        assert result.reason == "holiday"
        assert result.policy == "advance"
        assert result.holiday_name == "元旦"
        # 提前到 2025-12-31（周三）
        assert result.pay_date == date(2025, 12, 31)

    def test_advancing_across_a_month_keeps_the_period(self, session) -> None:
        """提前可能落到上一个月，但归属期间必须留在本月。

        否则"1 月的工资"会被记成"12 月"，年度同比与个税口径都会跟着错。
        """
        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        rule = _Rule(day_of_month=1, holiday_policy="advance")
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.pay_date.year == 2025
        assert result.period == "2026-01"

    def test_none_policy_keeps_a_non_workday(self, session) -> None:
        rule = _Rule(day_of_month=3, weekend_policy="none")
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.pay_date == JAN_3
        assert result.adjusted is False
        assert result.reason == "weekend"

    def test_day_31_clamps_to_month_end(self, session) -> None:
        """2 月没有 31 日 → 收敛到月末，而不是跳过这个月。"""
        rule = _Rule(day_of_month=31)
        result = workdays.resolve_payday(session, rule, 2026, 2)
        assert result.base_date == date(2026, 2, 28)

    def test_month_end_kind(self, session) -> None:
        rule = _Rule(day_kind="month_end")
        result = workdays.resolve_payday(session, rule, 2026, 2)
        assert result.base_date == date(2026, 2, 28)
        # 2026-02-28 是周六 → 提前到 27 日（周五）
        assert result.pay_date == date(2026, 2, 27)

    def test_last_workday_kind(self, session) -> None:
        """当月末个工作日：2026-01-31 是周六，因此是 30 日（周五）。"""
        rule = _Rule(day_kind="last_workday")
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.base_date == date(2026, 1, 30)
        assert result.pay_date == date(2026, 1, 30)

    def test_nth_workday_kind(self, session) -> None:
        rule = _Rule(day_kind="nth_workday", nth=3)
        result = workdays.resolve_payday(session, rule, 2026, 1)
        assert result.base_date == JAN_5

    def test_confidence_follows_the_calendar_data(self, session) -> None:
        """没录入 2026 年节假日时，结果必须**标明不确定**。

        否则用户会以为"系统算过了",而其实只是"1 日是周四所以看起来没问题"。
        """
        rule = _Rule(day_of_month=15)
        uncovered = workdays.resolve_payday(session, rule, 2026, 1)
        assert uncovered.confident is False

        workdays.upsert_override(session, JAN_1, is_workday=False, name="元旦")
        covered = workdays.resolve_payday(session, rule, 2026, 1)
        assert covered.confident is True

    def test_clamped_days_are_not_reported_as_confident(self, session) -> None:
        """`day_of_month=31` 在 2 月被收敛到 28 日时，也不能算"确定"。

        用户填的是 31，系统给的是 28 —— 这个改动必须被他看到。
        """
        workdays.upsert_override(session, date(2026, 2, 2), is_workday=False, name="占位")
        rule = _Rule(day_of_month=31)
        result = workdays.resolve_payday(session, rule, 2026, 2)
        assert result.confident is False


class TestUpcomingPaydays:
    def test_sorted_across_rules(self, session) -> None:
        """跨规则合并后按日期排序 —— 按规则逐个列出会让"最近的一次"
        淹没在列表里。"""
        early = _Rule(id=1, source_id=1, day_of_month=5)
        late = _Rule(id=2, source_id=2, day_of_month=25)
        results = workdays.upcoming_paydays(session, [late, early], start=JAN_1, months=2)
        dates = [item["pay_date"] for item in results]
        assert dates == sorted(dates)
        assert results[0]["source_id"] == 1

    def test_skips_dates_before_start(self, session) -> None:
        rule = _Rule(day_of_month=1)
        results = workdays.upcoming_paydays(session, [rule], start=JAN_15, months=2)
        assert all(item["pay_date"] >= "2026-01-15" for item in results)

    def test_disabled_rules_are_skipped(self, session) -> None:
        rule = _Rule(enabled=False)
        assert workdays.upcoming_paydays(session, [rule], start=JAN_1, months=1) == []

    def test_empty_rules_is_empty(self, session) -> None:
        assert workdays.upcoming_paydays(session, [], start=JAN_1, months=3) == []


# -----------------------------------------------------------------------------
# 批量录入
# -----------------------------------------------------------------------------
class TestImport:
    def test_parses_ranges_and_marks(self, session) -> None:
        text = """
        # 2026 年部分安排（示例）
        休 2026-01-01~2026-01-03 元旦
        班 2026-01-04
        """
        items, bad = workdays.parse_workday_text(text)
        assert bad == []
        assert len(items) == 4
        assert items[0]["day"] == JAN_1 and items[0]["is_workday"] is False
        assert items[-1]["day"] == JAN_4 and items[-1]["is_workday"] is True

    def test_default_mark_is_rest(self, session) -> None:
        """官方公告里绝大多数条目是放假，因此没写标记时按"休"处理。"""
        items, bad = workdays.parse_workday_text("2026-05-01 劳动节")
        assert bad == []
        assert items[0]["is_workday"] is False

    def test_unparseable_lines_are_reported_not_swallowed(self, session) -> None:
        """无法解析的行必须原样返回。

        他粘的是官方公告；如果有一行没被认出来，必须自己知道 ——
        否则那个假期会**悄悄变成工作日**。
        """
        items, bad = workdays.parse_workday_text("休 2026-01-01 元旦\n这是一行说明\n2026-13-45 不存在的日期")
        assert len(items) == 1
        assert len(bad) == 2

    def test_absurd_range_is_rejected(self, session) -> None:
        _, bad = workdays.parse_workday_text("休 2026-01-01~2026-12-31 一整年")
        assert len(bad) == 1

    def test_reversed_range_is_rejected(self, session) -> None:
        _, bad = workdays.parse_workday_text("休 2026-01-05~2026-01-01")
        assert len(bad) == 1

    def test_import_writes_and_makes_the_year_confident(self, session) -> None:
        result = workdays.import_workday_text(session, "休 2026-01-01~2026-01-03 元旦\n班 2026-01-04")
        assert result["saved"] == 4
        assert result["unparsed"] == []
        assert workdays.workday_fact(session, JAN_1).is_workday is False
        assert workdays.workday_fact(session, JAN_4).is_workday is True
        assert workdays.workday_fact(session, JAN_5).confident is True

    def test_empty_text_is_a_noop(self, session) -> None:
        result = workdays.import_workday_text(session, "")
        assert result == {"saved": 0, "unparsed": []}

    def test_zero_data_list_is_empty(self, session) -> None:
        assert workdays.list_overrides(session, year=2026) == []
        assert workdays.coverage_years(session) == set()
