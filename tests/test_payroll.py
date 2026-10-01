"""薪酬（P6）的用例。

三组重点：
1. **公式求值器的白名单边界** —— 工资表是可以被导入的数据，
   求值必须只有一份可见的运算白名单；
2. **两趟计算** —— 减项（个税、五险一金）基于**应发合计**算，
   一趟算完会让引用"应发"的减项拿到只加到一半的值，
   而这种错误不报错、只是数字偏小；
3. **收录记录的三个出口** —— 填写入账、本月跳过（必须给原因）、
   已入账则不可删改。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from accountbook.core.errors import ConflictError, NotFoundError, ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import payroll as payroll_service
from accountbook.services import transactions as transactions_service

PERIOD = "2026-10"
JAN = date(2026, 1, 1)


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


def _source(session, **kwargs):
    payload = {"name": "主职", "kind": "salary", "account_id": _account(session).id}
    payload.update(kwargs)
    return payroll_service.add_source(session, **payload)


# -----------------------------------------------------------------------------
# 公式求值
# -----------------------------------------------------------------------------
class TestFormula:
    def test_literals(self) -> None:
        assert payroll_service.evaluate_formula({"const": 12345}, {}) == 12345
        # 比例是万分比：1000 = 10%
        assert payroll_service.evaluate_formula({"ratio": 10000}, {}) == 1

    def test_variables_and_refs(self) -> None:
        context = {"basic": 1_000_000, "绩效": 200_000}
        assert payroll_service.evaluate_formula({"var": "basic"}, context) == 1_000_000
        assert payroll_service.evaluate_formula({"ref": "绩效"}, context) == 200_000

    def test_arithmetic(self) -> None:
        assert (
            payroll_service.evaluate_formula({"op": "add", "args": [{"const": 100}, {"const": 200}]}, {})
            == 300
        )
        assert (
            payroll_service.evaluate_formula({"op": "sub", "args": [{"const": 300}, {"const": 100}]}, {})
            == 200
        )

    def test_ratio_multiplication_rounds_half_up(self) -> None:
        """按比例算要四舍五入到最小单位，不能截断。

        0.005 元（半分）截断成 0 会让"按比例的三项"累计少几分钱，
        而工资表上差几分钱最容易引起争议。
        """
        value = payroll_service.evaluate_formula({"op": "mul", "args": [{"const": 1}, {"ratio": 5000}]}, {})
        assert value == 1  # 1 × 50% = 0.5 分 → 四舍五入到 1 分

    def test_min_max_caps(self) -> None:
        formula = {
            "op": "min",
            "args": [{"const": 150_000}, {"op": "mul", "args": [{"var": "basic"}, {"ratio": 5000}]}],
        }
        # 基本 10000 元，社保基数上限 1500 元
        assert payroll_service.evaluate_formula(formula, {"basic": 1_000_000}) == 150_000
        assert payroll_service.evaluate_formula(formula, {"basic": 100_000}) == 50_000

    def test_unknown_op_is_rejected(self) -> None:
        """**白名单**：不在表里的运算直接报错，而不是被忽略。

        静默忽略会让一项配置错误的扣款看起来"这个月没有"。
        """
        with pytest.raises(ValidationError, match="未知的运算"):
            payroll_service.evaluate_formula({"op": "pow", "args": [{"const": 2}]}, {})

    def test_unknown_variable_lists_what_is_available(self) -> None:
        with pytest.raises(ValidationError) as info:
            payroll_service.evaluate_formula({"var": "nope"}, {"basic": 1})
        assert info.value.details["available"] == ["basic"]

    def test_divide_by_zero_is_rejected(self) -> None:
        """除零在工资表里几乎总是配置错误（把比例填成 0）。"""
        with pytest.raises(ValidationError, match="除以零"):
            payroll_service.evaluate_formula({"op": "div", "args": [{"const": 100}, {"ratio": 0}]}, {})

    def test_depth_limit(self) -> None:
        """防的是导入的畸形 JSON 把求值拖进深递归。"""
        node: dict = {"const": 1}
        for _ in range(12):
            node = {"op": "add", "args": [node]}
        with pytest.raises(ValidationError, match="嵌套过深"):
            payroll_service.evaluate_formula(node, {})

    def test_bad_node_shapes_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="必须是对象"):
            payroll_service.evaluate_formula("基本工资", {})
        with pytest.raises(ValidationError, match="之一"):
            payroll_service.evaluate_formula({"wat": 1}, {})

    def test_empty_formula_is_zero(self) -> None:
        assert payroll_service.evaluate_formula({}, {}) == 0
        assert payroll_service.evaluate_formula(None, {}) == 0


# -----------------------------------------------------------------------------
# 来源与组成项
# -----------------------------------------------------------------------------
class TestSources:
    def test_zero_data_is_empty(self, session) -> None:
        assert payroll_service.list_sources(session) == []
        assert payroll_service.list_components(session) == []
        assert payroll_service.list_records(session) == []

    def test_create_and_validate(self, session) -> None:
        source = _source(session)
        assert source.id
        with pytest.raises(ValidationError, match="名称"):
            payroll_service.add_source(session, name="  ")
        with pytest.raises(ValidationError, match="未知的来源类型"):
            payroll_service.add_source(session, name="x", kind="nope")
        with pytest.raises(NotFoundError):
            payroll_service.add_source(session, name="x", account_id=999_999)

    def test_soft_delete_and_restore(self, session) -> None:
        source = _source(session)
        payroll_service.delete_source(session, source.id)
        assert payroll_service.list_sources(session) == []
        with pytest.raises(NotFoundError):
            payroll_service.get_source(session, source.id)
        restored = payroll_service.restore_source(session, source.id)
        assert restored.deleted_at is None

    def test_order_by_priority(self, session) -> None:
        low = _source(session, name="兼职", priority=1)
        high = _source(session, name="主职", priority=9)
        names = [row.name for row in payroll_service.list_sources(session)]
        assert names.index("主职") < names.index("兼职")
        assert low.id != high.id


class TestComponents:
    def test_validation(self, session) -> None:
        with pytest.raises(ValidationError, match="名称"):
            payroll_service.create_component(session, name="")
        with pytest.raises(ValidationError, match="未知的组成项类型"):
            payroll_service.create_component(session, name="x", kind="nope")
        with pytest.raises(ValidationError, match="未知的计算方式"):
            payroll_service.create_component(session, name="x", calc="magic")
        with pytest.raises(ValidationError, match="sign"):
            payroll_service.create_component(session, name="x", sign=0)
        with pytest.raises(ValidationError, match="不能为负"):
            payroll_service.create_component(session, name="x", amount_minor=-1)
        with pytest.raises(ValidationError, match="比例"):
            payroll_service.create_component(session, name="x", rate_bps=200_000)
        with pytest.raises(ValidationError, match="基数"):
            payroll_service.create_component(session, name="x", base_key="wat")

    def test_formula_is_validated_at_creation(self, session) -> None:
        """建的时候就试算一次，避免"保存成功但一算就报错"。"""
        with pytest.raises(ValidationError, match="未知的运算"):
            payroll_service.create_component(
                session, name="x", calc="formula", formula={"op": "pow", "args": []}
            )

    def test_list_includes_global_and_source_specific(self, session) -> None:
        source = _source(session)
        other = _source(session, name="兼职")
        global_item = payroll_service.create_component(session, name="个税", kind="tax", sign=-1)
        mine = payroll_service.create_component(session, name="基本工资", source_id=source.id)
        theirs = payroll_service.create_component(session, name="兼职费", source_id=other.id)
        ids = {row.id for row in payroll_service.list_components(session, source_id=source.id)}
        assert global_item.id in ids and mine.id in ids
        assert theirs.id not in ids


# -----------------------------------------------------------------------------
# 金额计算
# -----------------------------------------------------------------------------
class TestCompute:
    def _setup(self, session):
        source = _source(session)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=1_000_000,
            sort_order=1,
        )
        payroll_service.create_component(
            session,
            name="绩效",
            kind="performance",
            source_id=source.id,
            calc="ratio",
            base_key="basic",
            rate_bps=2000,
            sort_order=2,
        )
        return source

    def test_two_pass_computation(self, session) -> None:
        """**减项必须基于最终的应发合计。**

        个税按应发算，而应发包含绩效。一趟算完的话，
        个税会拿到"只加了基本工资"的值 —— 不报错，只是偏小。
        """
        source = self._setup(session)
        # 个税 = 应发 × 10%
        payroll_service.create_component(
            session,
            name="个税",
            kind="tax",
            sign=-1,
            calc="formula",
            formula={"op": "mul", "args": [{"var": "gross"}, {"ratio": 1000}]},
            source_id=source.id,
            sort_order=9,
        )
        result = payroll_service.compute_payroll(session, source.id)
        assert result["gross_minor"] == 1_200_000  # 10000 + 2000
        assert result["tax_minor"] == 120_000  # 应发的 10%，而不是基本工资的 10%
        assert result["net_minor"] == 1_080_000

    def test_basic_total_available_to_deductions(self, session) -> None:
        source = self._setup(session)
        payroll_service.create_component(
            session,
            name="公积金",
            kind="insurance",
            sign=-1,
            calc="ratio",
            base_key="basic",
            rate_bps=1200,
            source_id=source.id,
            sort_order=8,
        )
        result = payroll_service.compute_payroll(session, source.id)
        assert result["insurance_minor"] == 120_000  # 基本工资的 12%
        assert result["basic_minor"] == 1_000_000

    def test_named_reference_to_an_earlier_component(self, session) -> None:
        source = self._setup(session)
        payroll_service.create_component(
            session,
            name="年终奖预提",
            calc="formula",
            formula={"op": "mul", "args": [{"ref": "绩效"}, {"ratio": 5000}]},
            source_id=source.id,
            sort_order=5,
        )
        result = payroll_service.compute_payroll(session, source.id)
        bonus = next(item for item in result["items"] if item["name"] == "年终奖预提")
        assert bonus["amount_minor"] == 100_000  # 绩效 2000 的 50%

    def test_deduction_over_gross_is_refused(self, session) -> None:
        """减项把实发扣成负数说明配置有问题，而不是"这个月白干"。"""
        source = self._setup(session)
        payroll_service.create_component(
            session,
            name="离谱的扣款",
            kind="other",
            sign=-1,
            amount_minor=99_000_000,
            source_id=source.id,
            sort_order=9,
        )
        with pytest.raises(ConflictError, match="超过应发合计"):
            payroll_service.compute_payroll(session, source.id)

    def test_overrides_replace_computed_amounts(self, session) -> None:
        source = self._setup(session)
        components = payroll_service.list_components(session, source_id=source.id)
        performance = next(item for item in components if item.name == "绩效")
        result = payroll_service.compute_payroll(session, source.id, overrides={performance.id: 333_000})
        assert result["gross_minor"] == 1_333_000

    def test_disabled_components_are_skipped(self, session) -> None:
        source = self._setup(session)
        components = payroll_service.list_components(session, source_id=source.id)
        payroll_service.update_component(
            session, next(item for item in components if item.name == "绩效").id, enabled=False
        )
        assert payroll_service.compute_payroll(session, source.id)["gross_minor"] == 1_000_000

    def test_zero_components_yields_zero(self, session) -> None:
        source = _source(session)
        result = payroll_service.compute_payroll(session, source.id)
        assert result["gross_minor"] == 0
        assert result["net_minor"] == 0
        assert result["items"] == []

    def test_items_carry_how_they_were_computed(self, session) -> None:
        """明细里存计算方式，将来才说得清"这笔是怎么来的"。"""
        source = self._setup(session)
        result = payroll_service.compute_payroll(session, source.id)
        ratio_item = next(item for item in result["items"] if item["name"] == "绩效")
        assert ratio_item["calc"] == "ratio"
        assert ratio_item["rate_bps"] == 2000
        assert ratio_item["base_key"] == "basic"


# -----------------------------------------------------------------------------
# 收录记录
# -----------------------------------------------------------------------------
class TestRecords:
    def _prepared(self, session):
        source = _source(session)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=1_000_000,
            sort_order=1,
        )
        payroll_service.create_component(
            session,
            name="个税",
            kind="tax",
            sign=-1,
            amount_minor=100_000,
            source_id=source.id,
            sort_order=9,
        )
        return source

    def test_create_snapshots_the_computation(self, session) -> None:
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD)
        assert row.gross_minor == 1_000_000
        assert row.net_minor == 900_000
        assert row.tax_minor == 100_000
        assert len(row.items) == 2

    def test_duplicate_period_is_refused(self, session) -> None:
        source = self._prepared(session)
        payroll_service.create_record(session, source.id, PERIOD)
        with pytest.raises(ConflictError, match="已经有一条记录"):
            payroll_service.create_record(session, source.id, PERIOD)

    def test_period_format_is_validated(self, session) -> None:
        source = _source(session)
        for bad in ("2026", "2026-13", "2026/10", ""):
            with pytest.raises(ValidationError):
                payroll_service.create_record(session, source.id, bad)

    def test_snapshot_survives_template_changes(self, session) -> None:
        """模板改了，历史记录不该跟着变 —— 那正是存快照的理由。"""
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD)
        components = payroll_service.list_components(session, source_id=source.id)
        payroll_service.update_component(
            session,
            next(item for item in components if item.name == "基本工资").id,
            amount_minor=2_000_000,
        )
        assert payroll_service.get_record(session, row.id).gross_minor == 1_000_000

    def test_fill_creates_income_transaction_with_net_amount(self, session) -> None:
        """入账金额用**实发**：账户里真正到账的就是实发额。"""
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD, pay_date=date(2026, 10, 15))
        result = payroll_service.fill_record(session, row.id)
        assert result["created"] is True
        transaction = session.get(transactions_service.Transaction, result["transaction_id"])
        assert transaction.amount_minor == 900_000
        assert transaction.type == "income"
        assert transaction.direction == "in"
        assert payroll_service.get_record(session, row.id).status == "filled"

    def test_fill_is_idempotent(self, session) -> None:
        """重复点击不该生成第二笔流水。"""
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD, pay_date=date(2026, 10, 15))
        first = payroll_service.fill_record(session, row.id)
        second = payroll_service.fill_record(session, row.id)
        assert first["transaction_id"] == second["transaction_id"]
        assert second["created"] is False

    def test_fill_without_account_is_refused(self, session) -> None:
        source = _source(session, account_id=None)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=500_000,
            sort_order=1,
        )
        row = payroll_service.create_record(session, source.id, PERIOD)
        with pytest.raises(ValidationError, match="入账账户"):
            payroll_service.fill_record(session, row.id)

    def test_skip_requires_a_reason(self, session) -> None:
        """**合规出口**：只有"必须填"会让真的没工资的月份变成死锁，
        而用户会开始随手填假数据 —— 那比不填更糟。
        """
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD)
        with pytest.raises(ValidationError, match="原因"):
            payroll_service.skip_record(session, row.id, reason="  ")
        skipped = payroll_service.skip_record(session, row.id, reason="这个月没有工资")
        assert skipped.status == "skipped"
        assert skipped.skip_reason == "这个月没有工资"

    def test_filled_record_cannot_be_skipped_or_recomputed(self, session) -> None:
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD, pay_date=date(2026, 10, 15))
        payroll_service.fill_record(session, row.id)
        with pytest.raises(ConflictError, match="已入账"):
            payroll_service.skip_record(session, row.id, reason="反悔了")
        with pytest.raises(ConflictError, match="不能重算"):
            payroll_service.recompute_record(session, row.id)

    def test_draft_can_be_recomputed(self, session) -> None:
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD)
        components = payroll_service.list_components(session, source_id=source.id)
        payroll_service.update_component(
            session,
            next(item for item in components if item.name == "基本工资").id,
            amount_minor=1_500_000,
        )
        refreshed = payroll_service.recompute_record(session, row.id)
        assert refreshed.gross_minor == 1_500_000

    def test_filled_record_cannot_be_deleted(self, session) -> None:
        """删掉记录会让流水失去来源。"""
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD, pay_date=date(2026, 10, 15))
        payroll_service.fill_record(session, row.id)
        with pytest.raises(ConflictError, match="已经入账"):
            payroll_service.delete_record(session, row.id)

    def test_draft_can_be_deleted(self, session) -> None:
        source = self._prepared(session)
        row = payroll_service.create_record(session, source.id, PERIOD)
        payroll_service.delete_record(session, row.id)
        assert payroll_service.list_records(session) == []

    def test_ensure_record_is_idempotent(self, session) -> None:
        """调度器在发薪日调用它，不能覆盖用户已经改过的金额。"""
        source = self._prepared(session)
        first = payroll_service.ensure_record_for_period(session, source.id, PERIOD)
        payroll_service.update_record(session, first.id, gross_minor=777_000)
        again = payroll_service.ensure_record_for_period(session, source.id, PERIOD)
        assert again.id == first.id
        assert again.gross_minor == 777_000


# -----------------------------------------------------------------------------
# 发薪日与总览
# -----------------------------------------------------------------------------
class TestPaydayAndOverview:
    def test_without_a_rule_the_date_is_marked_inferred(self, session) -> None:
        """没配规则时给一个可继续的默认值，但必须标明是**推断**的。"""
        source = _source(session)
        payload = payroll_service.pay_date_for(session, source.id, PERIOD)
        assert payload["pay_date"] == "2026-10-15"
        assert payload["confidence"] == "inferred"

    def test_with_a_rule_the_confidence_comes_from_the_calendar(self, session) -> None:
        source = _source(session)
        payroll_service.upsert_payday_rule(session, source.id, day_of_month=15)
        payload = payroll_service.pay_date_for(session, source.id, PERIOD)
        # 2026 年的节假日还没录入 → 只能是 assumed
        assert payload["confidence"] == "assumed"

    def test_rule_validation(self, session) -> None:
        source = _source(session)
        with pytest.raises(ValidationError, match="发薪日类型"):
            payroll_service.upsert_payday_rule(session, source.id, day_kind="nope")
        with pytest.raises(ValidationError, match="调整策略"):
            payroll_service.upsert_payday_rule(session, source.id, weekend_policy="nope")
        with pytest.raises(ValidationError, match="1–31"):
            payroll_service.upsert_payday_rule(session, source.id, day_of_month=32)

    def test_overview_zero_data(self, session) -> None:
        body = payroll_service.payroll_overview(session, period=PERIOD, history_months=3)
        assert body["current"]["gross_minor"] == 0
        assert body["delta"]["gross"] is None
        # 月份要**补全**，否则折线图会把相隔半年的两个点连成直线
        assert len(body["months"]) == 3
        assert all(item["count"] == 0 for item in body["months"])

    def test_overview_totals_and_year_on_year(self, session) -> None:
        source = _source(session)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=1_000_000,
            sort_order=1,
        )
        this_year = payroll_service.create_record(session, source.id, PERIOD, pay_date=date(2026, 10, 15))
        payroll_service.fill_record(session, this_year.id, create_transaction=False)
        last_year = payroll_service.create_record(session, source.id, "2025-10", pay_date=date(2025, 10, 15))
        payroll_service.fill_record(session, last_year.id, create_transaction=False)

        body = payroll_service.payroll_overview(session, period=PERIOD, history_months=14)
        assert body["current"]["gross_minor"] == 1_000_000
        assert body["same_month_last_year"]["gross_minor"] == 1_000_000
        # 与去年同月相同 → 0% 而不是 None
        assert body["delta"]["gross"] == pytest.approx(0.0)

    def test_skipped_records_are_excluded_from_totals(self, session) -> None:
        source = _source(session)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=1_000_000,
            sort_order=1,
        )
        row = payroll_service.create_record(session, source.id, PERIOD)
        payroll_service.skip_record(session, row.id, reason="无工资")
        body = payroll_service.payroll_overview(session, period=PERIOD, history_months=1)
        assert body["current"]["gross_minor"] == 0

    def test_pending_records_respect_the_grace_period(self, session) -> None:
        """**发薪当天就弹窗拦截是把提醒做成了骚扰。**

        只有真的过了宽限期的才进待办队列。
        """
        source = _source(session)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=500_000,
            sort_order=1,
        )
        payroll_service.upsert_payday_rule(session, source.id, day_of_month=15, grace_days=3)
        today = datetime.now().date()
        # 今天发薪 → 不过宽限期 → 不拦截
        fresh = payroll_service.create_record(session, source.id, PERIOD, pay_date=today)
        assert payroll_service.pending_records(session) == []
        # 5 天前发薪、宽限 3 天 → 逾期 2 天 → 拦截
        payroll_service.update_record(session, fresh.id, pay_date=today - timedelta(days=5))
        pending = payroll_service.pending_records(session)
        assert len(pending) == 1
        assert pending[0]["overdue_days"] == 2

    def test_upcoming_paydays_carry_the_source_name(self, session) -> None:
        source = _source(session, name="主职")
        payroll_service.upsert_payday_rule(session, source.id, day_of_month=15)
        results = payroll_service.upcoming(session, start=JAN, months=2)
        assert results
        assert results[0]["source_name"] == "主职"
        dates = [item["pay_date"] for item in results]
        assert dates == sorted(dates)


class TestDraftExclusion:
    """**草稿不计入应发/实发。**

    "实发"的含义是"这笔钱到账了"，而草稿还没有 ——
    把草稿算进实发会让用户以为钱已经到了。
    报表那一节（`_section_payroll`）本来就是这么做的，这里必须一致。
    """

    def _source(self, session):
        from accountbook.services import accounts as accounts_service
        from accountbook.services import payroll as payroll_service

        account = accounts_service.list_accounts(session)[0]
        source = payroll_service.add_source(session, name="主职", account_id=account.id)
        payroll_service.create_component(
            session,
            name="基本工资",
            kind="basic",
            source_id=source.id,
            amount_minor=1_000_000,
            sort_order=1,
        )
        return source

    def test_draft_is_excluded_from_totals(self, session) -> None:
        from accountbook.services import payroll as payroll_service

        source = self._source(session)
        period = "2026-10"
        payroll_service.create_record(session, source.id, period, pay_date=date(2026, 10, 15))
        body = payroll_service.payroll_overview(session, period=period)

        # 草稿不进合计……
        assert body["current"]["gross_minor"] == 0
        assert body["current"]["net_minor"] == 0
        assert body["current"]["count"] == 0
        # ……但必须能被看到，否则用户会奇怪"工资表里明明有一条"
        assert body["draft_count"] == 1
        assert len(body["pending"]) == 1
        assert body["pending"][0]["period"] == period

    def test_filled_record_counts_and_clears_the_draft(self, session) -> None:
        from accountbook.services import payroll as payroll_service

        source = self._source(session)
        period = "2026-10"
        record = payroll_service.create_record(session, source.id, period, pay_date=date(2026, 10, 15))
        payroll_service.fill_record(session, record.id, create_transaction=False)
        body = payroll_service.payroll_overview(session, period=period)

        assert body["current"]["gross_minor"] == 1_000_000
        assert body["current"]["count"] == 1
        assert body["draft_count"] == 0
        assert body["pending"] == []

    def test_skipped_record_is_in_neither(self, session) -> None:
        """跳过的既不是已入账、也不是待填写。"""
        from accountbook.services import payroll as payroll_service

        source = self._source(session)
        period = "2026-10"
        record = payroll_service.create_record(session, source.id, period, pay_date=date(2026, 10, 15))
        payroll_service.skip_record(session, record.id, reason="这个月没有工资")
        body = payroll_service.payroll_overview(session, period=period)

        assert body["current"]["count"] == 0
        assert body["draft_count"] == 0
        assert body["pending"] == []
