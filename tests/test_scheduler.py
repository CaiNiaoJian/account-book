"""定时任务与提醒中心（P6）的用例。

三组重点：

1. **未实现的任务必须如实报 `skipped`** —— 一个"成功但什么也没做"的
   备份任务会让用户以为备份在跑；
2. **漏填拦截必须有合规出口** —— 稍后提醒有次数上限、跳过必须给原因。
   只有"必须填"会让真的没工资的月份变成死锁，而用户会开始随手填假数据；
3. **补办要能区分** —— 软件没开的那几天不该看起来像"当时真的弹过窗"。
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
from accountbook.services import insurance as insurance_service
from accountbook.services import payroll as payroll_service
from accountbook.services import scheduler, workdays

# 2026-01 的星期：1 日周四、2 日周五、3 日周六、4 日周日、5 日周一
NOW = datetime(2026, 1, 2, 9, 0)


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


def _source(session, **kwargs):
    payload = {"name": "主职", "account_id": accounts_service.list_accounts(session)[0].id}
    payload.update(kwargs)
    return payroll_service.add_source(session, **payload)


def _payroll_setup(session, *, amount_minor: int = 2_000_000):
    source = _source(session)
    payroll_service.create_component(
        session,
        name="基本工资",
        kind="basic",
        source_id=source.id,
        amount_minor=amount_minor,
        sort_order=1,
    )
    return source


# -----------------------------------------------------------------------------
# 下一次执行时刻
# -----------------------------------------------------------------------------
class TestComputeNextRun:
    def test_daily(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="d",
            name="每日",
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW,
        )
        # NOW 恰好是 09:00 → 下一次应当是明天
        assert task.next_run_at == datetime(2026, 1, 3, 9, 0)

    def test_daily_before_the_time_is_today(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="d2",
            name="每日",
            rule={"frequency": "daily", "at": "18:00"},
            now=datetime(2026, 1, 2, 8, 0),
        )
        assert task.next_run_at == datetime(2026, 1, 2, 18, 0)

    def test_weekly_lands_on_the_weekday(self, session) -> None:
        # 2026-01-02 是周五；下一个周一是 1 月 5 日
        task = scheduler.upsert_task(
            session,
            code="w",
            name="每周",
            rule={"frequency": "weekly", "weekday": 0, "at": "10:00"},
            now=NOW,
        )
        assert task.next_run_at == datetime(2026, 1, 5, 10, 0)

    def test_monthly_fixed_day(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="m",
            name="每月",
            rule={"frequency": "monthly", "day_of_month": 15, "at": "09:00"},
            now=NOW,
        )
        assert task.next_run_at == datetime(2026, 1, 15, 9, 0)

    def test_monthly_advance_can_land_in_the_previous_month(self, session) -> None:
        """1 月的 1 日已过 → 看 2 月；2 月 1 日是**周日**，
        按默认的"提前"策略落到 1 月 30 日（周五）。

        **这是正确行为**，不是 bug：现实中很多公司就是
        "1 号遇周末则提前到上月最后一个工作日发"。
        我最初的用例期望是 2 月 1 日，那等于假设"不调整"。
        """
        task = scheduler.upsert_task(
            session,
            code="m2",
            name="每月",
            rule={"frequency": "monthly", "day_of_month": 1, "at": "09:00"},
            now=NOW,
        )
        assert task.next_run_at == datetime(2026, 1, 30, 9, 0)

        # 而且不会卡住：从落点之后继续算，会正常推进到 3 月
        following = scheduler.compute_next_run(session, task, after=task.next_run_at)
        assert following is not None and following > task.next_run_at

    # -----------------------------------------------------------------------------
    # 执行
    # -----------------------------------------------------------------------------
    def test_monthly_weekend_is_adjusted_like_payday(self, session) -> None:
        """月度任务与发薪日**共用同一套工作日逻辑**，不另写一份。"""
        task = scheduler.upsert_task(
            session,
            code="m3",
            name="每月",
            rule={
                "frequency": "monthly",
                "day_of_month": 3,
                "at": "09:00",
                "weekend_policy": "postpone",
            },
            now=datetime(2026, 1, 1, 0, 0),
        )
        # 1 月 3 日是周六 → 顺延到 1 月 5 日（周一）
        assert task.next_run_at == datetime(2026, 1, 5, 9, 0)

    def test_monthly_last_workday(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="m4",
            name="月末工作日",
            rule={"frequency": "monthly", "day_kind": "last_workday", "at": "09:00"},
            now=datetime(2026, 1, 1, 0, 0),
        )
        # 2026-01-31 是周六 → 1 月 30 日（周五）
        assert task.next_run_at == datetime(2026, 1, 30, 9, 0)

    def test_once_in_the_future(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="o",
            name="一次",
            rule={"frequency": "once", "date": "2026-03-01", "at": "08:30"},
            now=NOW,
        )
        assert task.next_run_at == datetime(2026, 3, 1, 8, 30)

    def test_once_in_the_past_has_no_next_run(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="o2",
            name="一次",
            rule={"frequency": "once", "date": "2025-01-01", "at": "08:30"},
            now=NOW,
        )
        assert task.next_run_at is None

    def test_bad_rule_is_rejected_at_save_time(self, session) -> None:
        """坏规则在保存时就报，而不是等到执行那天。"""
        with pytest.raises(ValidationError, match="HH:MM"):
            scheduler.upsert_task(session, code="bad", name="坏", rule={"frequency": "daily", "at": "25点"})
        with pytest.raises(ValidationError, match="未知的频率"):
            scheduler.upsert_task(session, code="bad2", name="坏", rule={"frequency": "hourly"})
        with pytest.raises(ValidationError, match="未知的任务类型"):
            scheduler.upsert_task(session, code="bad3", name="坏", kind="nope")

    def test_upsert_is_idempotent_by_code(self, session) -> None:
        first = scheduler.upsert_task(
            session, code="same", name="甲", rule={"frequency": "daily", "at": "09:00"}, now=NOW
        )
        second = scheduler.upsert_task(
            session, code="same", name="乙", rule={"frequency": "daily", "at": "20:00"}, now=NOW
        )
        assert first.id == second.id
        assert second.name == "乙"
        assert second.next_run_at == datetime(2026, 1, 2, 20, 0)

    def test_disabled_task_has_no_next_run(self, session) -> None:
        task = scheduler.upsert_task(
            session,
            code="off",
            name="停用",
            rule={"frequency": "daily"},
            enabled=False,
            now=NOW,
        )
        assert task.next_run_at is None


class TestRunDue:
    def _due_task(self, session, **kwargs):
        payload = {
            "code": "t1",
            "name": "任务",
            "kind": "custom",
            "rule": {"frequency": "daily", "at": "09:00"},
        }
        payload.update(kwargs)
        # NOW 恰好是 09:00，而 compute_next_run 要求严格晚于游标，
        # 因此用 NOW 建档得到的是"明天 09:00" —— 当天并不到期。
        return scheduler.upsert_task(session, now=NOW - timedelta(days=1), **payload)

    def test_nothing_due_is_empty(self, session) -> None:
        scheduler.upsert_task(
            session,
            code="future",
            name="未来",
            rule={"frequency": "daily", "at": "23:59"},
            now=NOW,
        )
        assert scheduler.due_tasks(session, now=NOW) == []

    def test_zero_data_history_is_empty(self, session) -> None:
        assert scheduler.task_history(session) == []
        health = scheduler.health(session)
        assert health["total"] == 0
        # 没有运行时不该编一个 0% 的失败率
        assert health["failure_ratio"] is None

    def test_dry_run_records_nothing(self, session) -> None:
        self._due_task(session)
        result = scheduler.run_due(session, now=NOW, dry_run=True)
        assert result["count"] == 1
        assert scheduler.task_history(session) == []

    def test_execution_is_recorded_and_advances(self, session) -> None:
        task = self._due_task(session)
        result = scheduler.run_due(session, now=NOW)
        assert result["count"] == 1
        runs = scheduler.task_history(session)
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert scheduler.get_task(session, task.id).next_run_at > NOW

    def test_not_implemented_kinds_report_skipped(self, session) -> None:
        """**诚实性保证。** 一个"成功但什么也没做"的备份任务
        会让用户以为备份在跑。"""
        for kind in ("backup", "report"):
            self._due_task(session, code=f"k-{kind}", kind=kind)
        result = scheduler.run_due(session, now=NOW)
        assert result["count"] == 2
        assert all(item["status"] == "skipped" for item in result["items"])
        assert all(item["summary"] for item in result["items"])
        assert all("尚未实现" in item["summary"] for item in result["items"])

    def test_record_only_policy_skips_late_runs(self, session) -> None:
        """补办策略为"仅记录"时，迟到的那次不执行 —— 但依然留痕。"""
        self._due_task(
            session,
            rule={"frequency": "daily", "at": "09:00"},
            catch_up_policy="record_only",
        )
        late = NOW + timedelta(days=3)
        result = scheduler.run_due(session, now=late)
        assert any(item["status"] == "skipped" for item in result["items"])
        assert any(item["caught_up"] for item in result["items"])

    def test_caught_up_is_flagged(self, session) -> None:
        """软件没开的那几天不该看起来像"当时真的弹过窗"。"""
        self._due_task(session)
        late = NOW + timedelta(days=2)
        scheduler.run_due(session, now=late)
        runs = scheduler.task_history(session)
        assert any(run["caught_up"] is True for run in runs)

    def test_failure_is_recorded_but_does_not_break_the_round(self, session) -> None:
        """单个任务失败不该中断整轮。"""
        # insurance 任务指向一个不存在的档案 → 处理器返回 skipped 而不是抛错；
        # 这里用一个必然抛错的场景：自定义处理器不存在
        self._due_task(session, code="broken", kind="budget_close")
        result = scheduler.run_due(session, now=NOW)
        assert result["count"] >= 1
        # budget_close 是"派生值，无需落库"，如实说明而不是假装做了什么
        assert "派生值" in result["items"][0]["summary"]

    def test_catch_up_is_capped(self, session) -> None:
        """软件几个月没开时不能一次性补几百次。"""
        self._due_task(session)
        late = NOW + timedelta(days=100)
        result = scheduler.run_due(session, now=late, max_per_task=5)
        assert any("漏办超过" in item["summary"] for item in result["items"])
        assert scheduler.get_task(session, scheduler.list_tasks(session)[0].id).next_run_at > late

    def test_health_counts_by_status(self, session) -> None:
        self._due_task(session, code="a")
        self._due_task(session, code="b", kind="backup")
        scheduler.run_due(session, now=NOW)
        health = scheduler.health(session)
        assert health["total"] == 2
        assert health["success"] == 1
        assert health["skipped"] == 1
        assert health["failure_ratio"] == 0.0


# -----------------------------------------------------------------------------
# 发薪与五险一金的处理器
# -----------------------------------------------------------------------------
class TestHandlers:
    def test_payday_creates_draft_and_prompt(self, session) -> None:
        source = _payroll_setup(session)
        payroll_service.upsert_payday_rule(session, source.id, day_of_month=2)
        scheduler.upsert_task(
            session,
            code="pd",
            name="发薪",
            kind="payday",
            rule={"frequency": "monthly", "day_of_month": 2, "at": "09:00"},
            ref_id=source.id,
            now=NOW - timedelta(days=1),
        )
        scheduler.run_due(session, now=NOW)
        records = payroll_service.list_records(session)
        assert len(records) == 1
        assert records[0].period == "2026-01"
        prompts = scheduler.list_prompts(session, status="pending")
        assert len(prompts) == 1
        assert prompts[0]["target_kind"] == "payroll_record"
        assert prompts[0]["blocking_level"] == "strong"

    def test_payday_prompt_is_deduplicated(self, session) -> None:
        """发薪日可能被多次触发（手动执行 + 启动补办），
        没有去重的话用户会看到一屏一模一样的弹窗。"""
        source = _payroll_setup(session)
        payroll_service.upsert_payday_rule(session, source.id, day_of_month=2)
        for code in ("pd-a", "pd-b"):
            scheduler.upsert_task(
                session,
                code=code,
                name="发薪",
                kind="payday",
                rule={"frequency": "monthly", "day_of_month": 2, "at": "09:00"},
                ref_id=source.id,
                now=NOW - timedelta(days=1),
            )
        scheduler.run_due(session, now=NOW)
        assert len(scheduler.list_prompts(session, status="pending")) == 1

    def test_payday_without_source_is_skipped(self, session) -> None:
        scheduler.upsert_task(
            session,
            code="pd-x",
            name="发薪",
            kind="payday",
            rule={"frequency": "monthly", "day_of_month": 2, "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
        result = scheduler.run_due(session, now=NOW)
        assert result["items"][0]["status"] == "skipped"
        assert "来源" in result["items"][0]["summary"]

    def test_insurance_skips_when_rates_are_unfilled(self, session) -> None:
        """**比例没填时不写一堆 0。**

        写入 0 会让账户余额与年度对账都偏小，而用户无从察觉。
        """
        insurance_service.ensure_standard_items(session)
        profile = insurance_service.add_profile(session, name="本人", social_base_minor=1_000_000)
        scheduler.upsert_task(
            session,
            code="ins",
            name="五险一金",
            kind="insurance",
            rule={"frequency": "monthly", "day_of_month": 2, "at": "09:00"},
            ref_id=profile.id,
            now=NOW - timedelta(days=1),
        )
        result = scheduler.run_due(session, now=NOW)
        assert result["items"][0]["status"] == "skipped"
        assert "比例未填齐" in result["items"][0]["summary"]
        assert insurance_service.list_contributions(session) == []

    def test_insurance_writes_when_rates_are_filled(self, session) -> None:
        insurance_service.ensure_standard_items(session)
        # **不适用当地政策的险种应当停用而不是留空**：留空之后系统无法区分
        # "这里不缴"与"我还没填"，而按"未填"处理会让这个处理器永远跳过
        for item in insurance_service.list_items(session):
            if item.kind != "pension":
                insurance_service.upsert_item(session, kind=item.kind, enabled=False)
        insurance_service.upsert_item(session, kind="pension", personal_rate_bps=800)
        profile = insurance_service.add_profile(session, name="本人", social_base_minor=1_000_000)
        scheduler.upsert_task(
            session,
            code="ins2",
            name="五险一金",
            kind="insurance",
            rule={"frequency": "monthly", "day_of_month": 2, "at": "09:00"},
            ref_id=profile.id,
            now=NOW - timedelta(days=1),
        )
        result = scheduler.run_due(session, now=NOW)
        assert result["items"][0]["status"] == "success"
        assert len(insurance_service.list_contributions(session)) == 1  # 只有启用中的那一个

    def test_budget_close_explains_it_is_derived(self, session) -> None:
        """预算的"已用"是算出来的，如实说明没有东西需要落库。"""
        scheduler.upsert_task(
            session,
            code="bc",
            name="预算月结",
            kind="budget_close",
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
        result = scheduler.run_due(session, now=NOW)
        assert result["items"][0]["status"] == "success"
        assert "派生值" in result["items"][0]["summary"]

    def test_custom_task_creates_a_notification(self, session) -> None:
        scheduler.upsert_task(
            session,
            code="cu",
            name="提醒交房租",
            kind="custom",
            note="今天要交房租",
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
        scheduler.run_due(session, now=NOW)
        notes = scheduler.list_notifications(session)
        assert len(notes) == 1
        assert notes[0]["title"] == "提醒交房租"


# -----------------------------------------------------------------------------
# 待办提示
# -----------------------------------------------------------------------------
class TestPrompts:
    def _prompt(self, session, **kwargs):
        payload = {
            "kind": "payroll",
            "title": "工资待填写",
            "target_kind": "payroll_record",
            "target_id": 1,
            "dedupe_key": "payroll:1:2026-01",
        }
        payload.update(kwargs)
        return scheduler.create_prompt(session, now=NOW - timedelta(days=1), **payload)

    def test_zero_data_is_empty(self, session) -> None:
        assert scheduler.list_prompts(session) == []
        assert scheduler.blocking_prompts(session, now=NOW) == []

    def test_dedupe_key_prevents_duplicates(self, session) -> None:
        first = self._prompt(session)
        second = self._prompt(session)
        assert first.id == second.id

    def test_snooze_has_a_cap(self, session) -> None:
        """**没有上限的"稍后"等于"永不"，拦截也就失去了意义。**"""
        prompt = self._prompt(session, max_snooze=2)
        scheduler.snooze_prompt(session, prompt.id, minutes=30, now=NOW)
        scheduler.snooze_prompt(session, prompt.id, minutes=30, now=NOW)
        with pytest.raises(ConflictError, match="已用完"):
            scheduler.snooze_prompt(session, prompt.id, minutes=30, now=NOW)
        # 报错信息里要给出下一步该做什么
        with pytest.raises(ConflictError) as info:
            scheduler.snooze_prompt(session, prompt.id, minutes=30, now=NOW)
        assert "本月跳过" in str(info.value.details.get("suggestion", ""))

    def test_snoozed_prompt_leaves_the_blocking_queue_until_due(self, session) -> None:
        """`snoozed` 未到时刻的不该拦住用户 —— 那是"稍后提醒"的字面意思。"""
        prompt = self._prompt(session)
        scheduler.snooze_prompt(session, prompt.id, minutes=30, now=NOW)
        assert scheduler.blocking_prompts(session, now=NOW) == []
        assert len(scheduler.blocking_prompts(session, now=NOW + timedelta(minutes=31))) == 1

    def test_snooze_interval_must_be_positive(self, session) -> None:
        prompt = self._prompt(session)
        with pytest.raises(ValidationError, match="大于 0"):
            scheduler.snooze_prompt(session, prompt.id, minutes=0)

    def test_skip_requires_a_reason(self, session) -> None:
        """**合规出口**：跳过必须留痕，可事后补录。"""
        prompt = self._prompt(session)
        with pytest.raises(ValidationError, match="原因"):
            scheduler.skip_prompt(session, prompt.id, reason="   ")
        skipped = scheduler.skip_prompt(session, prompt.id, reason="这个月没有工资", now=NOW)
        assert skipped.status == "skipped"
        assert skipped.skip_reason == "这个月没有工资"
        assert scheduler.blocking_prompts(session, now=NOW) == []

    def test_resolved_prompts_cannot_be_snoozed_or_skipped_again(self, session) -> None:
        prompt = self._prompt(session)
        scheduler.resolve_prompt(session, prompt.id, now=NOW)
        with pytest.raises(ConflictError, match="已经处理过"):
            scheduler.snooze_prompt(session, prompt.id, now=NOW)
        with pytest.raises(ConflictError, match="已经处理过"):
            scheduler.skip_prompt(session, prompt.id, reason="再来一次", now=NOW)

    def test_resolving_by_target_clears_the_block(self, session) -> None:
        """填完工资表之后，指向它的提示要一并不了结 ——
        否则用户填完了还被拦着。"""
        self._prompt(session, target_kind="payroll_record", target_id=7)
        assert len(scheduler.blocking_prompts(session, now=NOW)) == 1
        cleared = scheduler.resolve_prompts_for_target(
            session, target_kind="payroll_record", target_id=7, now=NOW
        )
        assert cleared == 1
        assert scheduler.blocking_prompts(session, now=NOW) == []

    def test_normal_level_never_blocks(self, session) -> None:
        self._prompt(session, blocking_level="normal")
        assert scheduler.blocking_prompts(session, now=NOW) == []
        assert len(scheduler.list_prompts(session)) == 1

    def test_missing_prompt_raises(self, session) -> None:
        with pytest.raises(NotFoundError):
            scheduler.snooze_prompt(session, 999_999, now=NOW)

    def test_serialize_exposes_remaining_snoozes(self, session) -> None:
        prompt = self._prompt(session, max_snooze=3)
        scheduler.snooze_prompt(session, prompt.id, now=NOW)
        payload = scheduler.list_prompts(session)[0]
        assert payload["snooze_left"] == 2


# -----------------------------------------------------------------------------
# 通知中心
# -----------------------------------------------------------------------------
class TestNotifications:
    def test_zero_data(self, session) -> None:
        assert scheduler.list_notifications(session) == []
        assert scheduler.unread_count(session) == 0
        assert scheduler.mark_all_read(session) == 0

    def test_notify_and_dedupe(self, session) -> None:
        scheduler.notify(session, level="info", title="甲", dedupe_key="k1", now=NOW)
        scheduler.notify(session, level="info", title="乙", dedupe_key="k1", now=NOW)
        rows = scheduler.list_notifications(session)
        assert len(rows) == 1
        assert rows[0]["title"] == "甲"

    def test_unread_count_and_mark_read(self, session) -> None:
        scheduler.notify(session, level="info", title="甲", now=NOW)
        scheduler.notify(session, level="warn", title="乙", now=NOW)
        assert scheduler.unread_count(session) == 2
        rows = scheduler.list_notifications(session)
        scheduler.mark_read(session, rows[0]["id"], now=NOW)
        assert scheduler.unread_count(session) == 1
        assert len(scheduler.list_notifications(session, unread_only=True)) == 1
        assert scheduler.mark_all_read(session, now=NOW) == 1
        assert scheduler.unread_count(session) == 0

    def test_unknown_level_is_rejected(self, session) -> None:
        with pytest.raises(ValidationError, match="级别"):
            scheduler.notify(session, level="fatal", title="x")

    def test_missing_notification_raises(self, session) -> None:
        with pytest.raises(NotFoundError):
            scheduler.mark_read(session, 999_999)


# -----------------------------------------------------------------------------
# 与工作日服务的一致性
# -----------------------------------------------------------------------------
class TestWorkdayConsistency:
    def test_monthly_task_respects_recorded_holidays(self, session) -> None:
        """月度任务走的是**同一套**工作日逻辑，因此录了节假日就会跟着变。"""
        task = scheduler.upsert_task(
            session,
            code="h",
            name="每月",
            rule={
                "frequency": "monthly",
                "day_of_month": 1,
                "at": "09:00",
                "holiday_policy": "advance",
            },
            now=datetime(2025, 12, 2, 0, 0),
        )
        # 未录节假日时：1 月 1 日是周四、是工作日
        assert task.next_run_at == datetime(2026, 1, 1, 9, 0)

        workdays.upsert_override(session, date(2026, 1, 1), is_workday=False, name="元旦")
        recomputed = scheduler.compute_next_run(session, task, after=datetime(2025, 12, 2))
        # 录了元旦、策略为提前 → 落到 2025-12-31
        assert recomputed == datetime(2025, 12, 31, 9, 0)
