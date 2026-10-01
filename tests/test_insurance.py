"""五险一金（P6）的用例。

重点在三件事：

1. **字典只铺名称、不铺比例** —— 比例因城市与年份而异，写死等于给一个
   看起来权威、实际只对某市某年成立的数字；
2. **保底与封顶必须报出来** —— 用户填 3 万基数、系统按 2.4 万算，
   不说明他会以为软件算错了；
3. **余额是算出来的** —— 利息由用户录（利率不编），提取是一条记录而不是改余额。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from accountbook.core.errors import ConflictError, NotFoundError, ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import insurance

PERIOD = "2026-10"


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


def _items(session):
    return insurance.ensure_standard_items(session)


def _profile(session, **kwargs):
    payload = {"name": "本人", "social_base_minor": 1_000_000, "housing_base_minor": 1_000_000}
    payload.update(kwargs)
    return insurance.add_profile(session, **payload)


def _item_id(session, kind: str) -> int:
    return next(row.id for row in _items(session) if row.kind == kind)


# -----------------------------------------------------------------------------
# 字典
# -----------------------------------------------------------------------------
class TestItems:
    def test_zero_data_is_empty_then_seeded_without_rates(self, session) -> None:
        """**字典只铺名称，比例一律留 0。**

        这是本文件最重要的一条：比例因城市与年份而异，
        写死一套等于给用户一个看起来权威、实际只对某市某年成立的数字。
        """
        assert insurance.list_items(session) == []
        items = _items(session)
        names = [row.name for row in items]
        assert "养老保险" in names and "住房公积金" in names
        assert all(row.personal_rate_bps == 0 for row in items)
        assert all(row.employer_rate_bps == 0 for row in items)
        # 界面上要能直接看出"还没填"
        assert all(row.__dict__ and not insurance.serialize_item(row)["rates_filled"] for row in items)

    def test_structure_flags_are_factual(self, session) -> None:
        """哪些进个人账户、哪些个人不缴 —— 这些是**制度性事实**，可以写进字典。"""
        by_kind = {row.kind: row for row in _items(session)}
        assert by_kind["pension"].personal_to_account is True
        assert by_kind["pension"].employer_to_account is False
        assert by_kind["housing_fund"].personal_to_account is True
        assert by_kind["housing_fund"].employer_to_account is True
        assert by_kind["housing_fund"].use_housing_base is True
        assert "个人不缴" in by_kind["injury"].note

    def test_seeding_is_idempotent_and_keeps_filled_rates(self, session) -> None:
        """**用户填完比例之后再调用它，不能把比例清掉。**"""
        _items(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        _items(session)
        row = next(item for item in insurance.list_items(session) if item.kind == "pension")
        assert row.personal_rate_bps == 800

    def test_upsert_validation(self, session) -> None:
        with pytest.raises(ValidationError, match="未知的险种"):
            insurance.upsert_item(session, kind="nope")
        with pytest.raises(ValidationError, match="比例"):
            insurance.upsert_item(session, kind="pension", personal_rate_bps=20000)
        with pytest.raises(ValidationError, match="基数不能为负"):
            insurance.upsert_item(session, kind="pension", cap_base_minor=-1)
        with pytest.raises(ValidationError, match="保底基数不能高于封顶"):
            insurance.upsert_item(session, kind="pension", floor_base_minor=500, cap_base_minor=100)

    def test_delete_blocked_when_used(self, session) -> None:
        """删掉会让历史缴纳记录失去险种，余额与年度对账都会跟着错。"""
        profile = _profile(session)
        pension = _item_id(session, "pension")
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, cap_base_minor=500_000)
        insurance.record_contribution(session, profile.id, PERIOD)
        with pytest.raises(ConflictError, match="不能删除"):
            insurance.delete_item(session, pension)

    def test_delete_unused_is_allowed(self, session) -> None:
        _items(session)
        target = _item_id(session, "critical_illness")
        insurance.delete_item(session, target)
        assert all(row.id != target for row in insurance.list_items(session))


# -----------------------------------------------------------------------------
# 档案
# -----------------------------------------------------------------------------
class TestProfiles:
    def test_create_and_validate(self, session) -> None:
        profile = _profile(session)
        assert profile.id
        with pytest.raises(ValidationError, match="名称"):
            insurance.add_profile(session, name="  ")
        with pytest.raises(ValidationError, match="不能为负"):
            insurance.add_profile(session, name="x", social_base_minor=-1)
        with pytest.raises(ValidationError, match="结束日期"):
            insurance.add_profile(
                session,
                name="x",
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 1, 1),
            )

    def test_soft_delete_and_restore(self, session) -> None:
        profile = _profile(session)
        insurance.delete_profile(session, profile.id)
        assert insurance.list_profiles(session) == []
        with pytest.raises(NotFoundError):
            insurance.get_profile(session, profile.id)
        assert insurance.restore_profile(session, profile.id).deleted_at is None

    def test_serialize_includes_account_balance(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_item(session, kind="housing_fund", personal_rate_bps=1200, employer_rate_bps=1200)
        insurance.record_contribution(session, profile.id, PERIOD)
        payload = insurance.serialize_profile(session, profile)
        # 公积金双方各 12% × 基数 1 万 = 1200 + 1200 = 2400 元
        assert payload["account_balance_minor"] == 240_000


# -----------------------------------------------------------------------------
# 缴纳计算
# -----------------------------------------------------------------------------
class TestCompute:
    def test_zero_rates_produce_zero_and_report_incomplete(self, session) -> None:
        """比例还没填时合计必然是 0 —— **必须让调用方知道**，
        否则界面上会显示一个像"这个月没缴"的 0。"""
        profile = _profile(session)
        # 字典还没铺时也算不完整 —— 否则界面会把"0 元合计"显示成
        # "这个月没缴"，而真相是"还没配置"
        assert insurance.compute_contribution(session, profile.id)["incomplete"] is True

        _items(session)
        computed = insurance.compute_contribution(session, profile.id)
        assert computed["personal_total_minor"] == 0
        assert computed["incomplete"] is True
        assert "养老保险" in computed["unfilled_items"]

    def test_rates_are_applied_to_the_right_base(self, session) -> None:
        """公积金用公积金基数，社保用社保基数 —— 很多城市这两者不同。"""
        profile = _profile(session, social_base_minor=2_000_000, housing_base_minor=1_000_000)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        insurance.upsert_item(session, kind="housing_fund", personal_rate_bps=1200, employer_rate_bps=1200)
        computed = insurance.compute_contribution(session, profile.id)
        by_kind = {item["kind"]: item for item in computed["items"]}
        assert by_kind["pension"]["base_minor"] == 2_000_000
        assert by_kind["pension"]["personal_minor"] == 160_000
        assert by_kind["housing_fund"]["base_minor"] == 1_000_000
        assert by_kind["housing_fund"]["personal_minor"] == 120_000

    def test_cap_is_applied_and_reported(self, session) -> None:
        """**收敛必须报出来。** 用户填 3 万基数、系统按 2.4 万算，
        不说明他会以为软件算错了。"""
        profile = _profile(session, social_base_minor=3_000_000)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, cap_base_minor=2_400_000)
        computed = insurance.compute_contribution(session, profile.id)
        pension = next(item for item in computed["items"] if item["kind"] == "pension")
        assert pension["raw_base_minor"] == 3_000_000
        assert pension["base_minor"] == 2_400_000
        assert pension["clamped"] == "cap"

    def test_floor_is_applied_and_reported(self, session) -> None:
        profile = _profile(session, social_base_minor=100_000)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, floor_base_minor=500_000)
        computed = insurance.compute_contribution(session, profile.id)
        pension = next(item for item in computed["items"] if item["kind"] == "pension")
        assert pension["base_minor"] == 500_000
        assert pension["clamped"] == "floor"

    def test_no_limits_means_no_clamp(self, session) -> None:
        profile = _profile(session, social_base_minor=9_999_999)
        computed = insurance.compute_contribution(session, profile.id)
        assert all(item["clamped"] == "" for item in computed["items"])

    def test_to_account_semantics(self, session) -> None:
        """养老只有**个人**部分进账户；公积金**双方**都进。"""
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, employer_rate_bps=1600)
        insurance.upsert_item(session, kind="housing_fund", personal_rate_bps=1200, employer_rate_bps=1200)
        computed = insurance.compute_contribution(session, profile.id)
        by_kind = {item["kind"]: item for item in computed["items"]}
        assert by_kind["pension"]["to_account_minor"] == 80_000  # 只有个人 8%
        assert by_kind["housing_fund"]["to_account_minor"] == 240_000  # 双方各 12%
        assert computed["to_account_total_minor"] == 320_000

    def test_unemployment_does_not_enter_the_account(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_item(session, kind="unemployment", personal_rate_bps=50, employer_rate_bps=50)
        computed = insurance.compute_contribution(session, profile.id)
        row = next(item for item in computed["items"] if item["kind"] == "unemployment")
        assert row["personal_minor"] == 5_000
        assert row["to_account_minor"] == 0


# -----------------------------------------------------------------------------
# 缴纳记录
# -----------------------------------------------------------------------------
class TestContributions:
    def test_record_is_idempotent(self, session) -> None:
        """**重复写入会让余额凭空翻倍。**

        调度器与工资收录都可能触发它，因此幂等必须由数据结构保证。
        """
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        insurance.record_contribution(session, profile.id, PERIOD)
        insurance.record_contribution(session, profile.id, PERIOD)
        rows = insurance.list_contributions(session, profile_id=profile.id, period=PERIOD)
        assert len(rows) == 1
        assert insurance.account_balances(session, profile_id=profile.id)["pension"] == 80_000

    def test_overwrite_updates_in_place(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        insurance.record_contribution(session, profile.id, PERIOD)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=1600)
        insurance.record_contribution(session, profile.id, PERIOD, overwrite=True)
        rows = insurance.list_contributions(session, profile_id=profile.id, period=PERIOD)
        assert len(rows) == 1
        assert rows[0].personal_minor == 160_000

    def test_snapshot_keeps_the_rate_used(self, session) -> None:
        """比例每年会变，"去年 3 月按什么比例扣的"必须能原样复现。"""
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        insurance.record_contribution(session, profile.id, PERIOD)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=1600)
        rows = insurance.list_contributions(session, profile_id=profile.id, period=PERIOD)
        assert rows[0].personal_rate_bps == 800
        assert rows[0].personal_minor == 80_000

    def test_period_format_is_validated(self, session) -> None:
        profile = _profile(session)
        for bad in ("2026", "2026-13", "2026/10"):
            with pytest.raises(ValidationError):
                insurance.record_contribution(session, profile.id, bad)

    def test_zero_data_balances_are_empty(self, session) -> None:
        profile = _profile(session)
        assert insurance.account_balances(session, profile_id=profile.id) == {}
        assert insurance.list_contributions(session) == []


# -----------------------------------------------------------------------------
# 余额与提取
# -----------------------------------------------------------------------------
class TestBalancesAndWithdrawals:
    def _funded(self, session):
        profile = _profile(session)
        insurance.upsert_item(session, kind="housing_fund", personal_rate_bps=1200, employer_rate_bps=1200)
        insurance.record_contribution(session, profile.id, PERIOD)
        return profile

    def test_balance_is_derived(self, session) -> None:
        profile = self._funded(session)
        # 双方各 12% × 1 万 = 2400 元
        assert insurance.account_balances(session, profile_id=profile.id) == {"housing_fund": 240_000}

    def test_withdrawal_reduces_the_balance(self, session) -> None:
        profile = self._funded(session)
        item = _item_id(session, "housing_fund")
        insurance.add_withdrawal(
            session,
            profile.id,
            item_id=item,
            amount_minor=100_000,
            occurred_at=date(2026, 10, 20),
            reason="rent",
        )
        assert insurance.account_balances(session, profile_id=profile.id)["housing_fund"] == 140_000

    def test_over_withdrawal_is_refused(self, session) -> None:
        """余额是算出来的，提超了会让余额变成负数 —— 那不是有意义的账户状态。"""
        profile = self._funded(session)
        item = _item_id(session, "housing_fund")
        with pytest.raises(ConflictError, match="余额不足"):
            insurance.add_withdrawal(
                session,
                profile.id,
                item_id=item,
                amount_minor=999_999,
                occurred_at=date(2026, 10, 20),
            )

    def test_withdrawal_validation(self, session) -> None:
        profile = self._funded(session)
        item = _item_id(session, "housing_fund")
        with pytest.raises(ValidationError, match="大于 0"):
            insurance.add_withdrawal(
                session, profile.id, item_id=item, amount_minor=0, occurred_at=date(2026, 10, 20)
            )
        with pytest.raises(ValidationError, match="未知的提取原因"):
            insurance.add_withdrawal(
                session,
                profile.id,
                item_id=item,
                amount_minor=1_000,
                occurred_at=date(2026, 10, 20),
                reason="nope",
            )

    def test_delete_withdrawal_restores_the_balance(self, session) -> None:
        profile = self._funded(session)
        item = _item_id(session, "housing_fund")
        row = insurance.add_withdrawal(
            session,
            profile.id,
            item_id=item,
            amount_minor=100_000,
            occurred_at=date(2026, 10, 20),
        )
        insurance.delete_withdrawal(session, row.id)
        assert insurance.account_balances(session, profile_id=profile.id)["housing_fund"] == 240_000

    def test_interest_from_the_statement_enters_the_balance(self, session) -> None:
        """利息由用户录 —— 利率因城市与年份而异，我不编。"""
        profile = self._funded(session)
        before = insurance.account_balances(session, profile_id=profile.id)["housing_fund"]
        insurance.upsert_statement(session, profile.id, 2026, interest_minor=5_000)
        after = insurance.account_balances(session, profile_id=profile.id)["housing_fund"]
        assert after == before + 5_000


# -----------------------------------------------------------------------------
# 年度对账
# -----------------------------------------------------------------------------
class TestAnnualStatement:
    def _year(self, session, periods=("2026-01", "2026-02")):
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, employer_rate_bps=1600)
        for period in periods:
            insurance.record_contribution(session, profile.id, period)
        return profile

    def test_totals_and_missing_months(self, session) -> None:
        profile = self._year(session)
        body = insurance.annual_statement(session, profile.id, 2026)
        assert body["computed_personal_minor"] == 160_000  # 两期各 800 元
        assert body["computed_employer_minor"] == 320_000
        assert body["months_recorded"] == 2
        # 有 10 个月没记录 —— 差异很可能来自这里，直接说明省得用户自己猜
        assert body["missing_months"] == 10

    def test_difference_is_none_until_expected_values_are_entered(self, session) -> None:
        profile = self._year(session)
        body = insurance.annual_statement(session, profile.id, 2026)
        assert body["difference"]["personal_minor"] is None
        assert body["expected"]["personal_minor"] is None

    def test_difference_reports_the_gap(self, session) -> None:
        profile = self._year(session)
        insurance.upsert_statement(session, profile.id, 2026, expected_personal_minor=170_000)
        body = insurance.annual_statement(session, profile.id, 2026)
        # 系统算出 1600 元，对账单说 1700 元 → 差 −100 元
        assert body["difference"]["personal_minor"] == -10_000

    def test_zero_data_statement_is_buildable(self, session) -> None:
        profile = _profile(session)
        body = insurance.annual_statement(session, profile.id, 2026)
        assert body["items"] == []
        assert body["computed_personal_minor"] == 0
        assert body["months_recorded"] == 0
        assert body["missing_months"] == 12

    def test_statement_is_upserted_not_duplicated(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_statement(session, profile.id, 2026, interest_minor=100)
        insurance.upsert_statement(session, profile.id, 2026, interest_minor=200)
        body = insurance.annual_statement(session, profile.id, 2026)
        assert body["interest_minor"] == 200

    def test_year_is_bounded(self, session) -> None:
        profile = _profile(session)
        with pytest.raises(ValidationError, match="年份"):
            insurance.upsert_statement(session, profile.id, 1200)


# -----------------------------------------------------------------------------
# 总览
# -----------------------------------------------------------------------------
class TestOverview:
    def test_zero_data(self, session) -> None:
        body = insurance.insurance_overview(session, start_period="2026-01", end_period="2026-12")
        assert body["personal_total_minor"] == 0
        assert body["by_kind"] == []
        assert body["by_period"] == []
        # 没有数据时不该编一个 0% 的"单位占比"
        assert body["employer_share"] is None

    def test_totals_by_kind_and_period(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800, employer_rate_bps=1600)
        insurance.upsert_item(session, kind="housing_fund", personal_rate_bps=1200, employer_rate_bps=1200)
        for period in ("2026-01", "2026-02"):
            insurance.record_contribution(session, profile.id, period)
        body = insurance.insurance_overview(session, start_period="2026-01", end_period="2026-02")
        # 个人 8% + 12% = 2000 元/月 × 2；单位 16% + 12% = 2800 元/月 × 2
        assert body["personal_total_minor"] == 400_000
        assert body["employer_total_minor"] == 560_000
        assert body["total_minor"] == 960_000
        assert len(body["by_period"]) == 2
        # 单位缴纳是"隐形收入"，占比要算对
        assert body["employer_share"] == pytest.approx(560_000 / 960_000)
        kinds = [item["kind"] for item in body["by_kind"]]
        assert kinds[0] == "pension"  # 金额大的排前面

    def test_by_year_aggregates_across_months(self, session) -> None:
        """**按年聚合**：缴纳是长期积累，而跨年的比例调整只有在按年图上才看得出来。

        与 `by_period` 各说一件事 —— 按月看节奏，按年看趋势。
        """
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        for period in ("2025-11", "2025-12", "2026-01", "2026-02"):
            insurance.record_contribution(session, profile.id, period)
        body = insurance.insurance_overview(session, start_period="2025-01", end_period="2026-12")
        years = {row["year"]: row for row in body["by_year"]}
        assert sorted(years) == ["2025", "2026"]
        # 养老个人 8% x 10000 = 800 元/月
        assert years["2025"]["personal_minor"] == 160_000
        assert years["2026"]["personal_minor"] == 160_000
        # 四个月都在，但分成两年
        assert len(body["by_period"]) == 4

    def test_by_year_is_empty_without_records(self, session) -> None:
        _profile(session)
        body = insurance.insurance_overview(session, start_period="2026-01", end_period="2026-12")
        assert body["by_year"] == []

    def test_range_filters_periods(self, session) -> None:
        profile = _profile(session)
        insurance.upsert_item(session, kind="pension", personal_rate_bps=800)
        insurance.record_contribution(session, profile.id, "2026-01")
        insurance.record_contribution(session, profile.id, "2026-06")
        body = insurance.insurance_overview(session, start_period="2026-05", end_period="2026-12")
        assert [item["period"] for item in body["by_period"]] == ["2026-06"]
