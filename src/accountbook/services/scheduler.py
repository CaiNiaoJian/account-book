"""定时任务与提醒中心（P6 / 需求 19）。

三条设计原则
============

**一、错过了要知道，并且要能补。**
软件不常驻，发薪日那天很可能根本没开。因此 `catch_up_policy` 是显式的：
`startup`（启动时补办）/ `immediate`（立即补）/ `record_only`（仅记录）。
补办出来的执行留痕带 `caught_up=True` —— 用户必须能区分
"当时就跑了"与"事后补的"，否则他会以为发薪日当天真的弹过窗。

**二、没实现的任务要如实报 `skipped`，不能假装成功。**
备份、报表生成、账单日这些依赖后续阶段。一个"成功但什么也没做"的任务
比一个明确说"还没实现"的任务危险得多 —— 前者会让用户以为备份在跑。

**三、强弹必须有合规出口。**
只有"必须填"会让真的没工资的月份变成**死锁**，而用户会开始随手填假数据，
那比不填更糟。因此：`snooze` 有次数上限（到顶后只剩"填"或"跳过"），
`skip` 必须给原因并留痕，两者都可事后补录。
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import (
    InsuranceProfile,
    Notification,
    PaydayRule,
    PaySource,
    PendingPrompt,
    ScheduledTask,
    TaskRun,
)
from . import insurance as insurance_service
from . import payroll as payroll_service
from . import workdays

__all__ = [
    "TASK_KINDS",
    "blocking_prompts",
    "compute_next_run",
    "create_prompt",
    "delete_task",
    "due_tasks",
    "list_notifications",
    "list_prompts",
    "list_tasks",
    "mark_all_read",
    "mark_read",
    "notify",
    "resolve_prompt",
    "run_due",
    "run_task_now",
    "skip_prompt",
    "snooze_prompt",
    "task_history",
    "unread_count",
    "upsert_task",
]

_logger = logging.getLogger(__name__)

TASK_KINDS = (
    "payday",
    "insurance",
    "bill",
    "repayment",
    "recurring",
    "budget_close",
    "report",
    "backup",
    "custom",
)

#: 这些类型的处理器依赖后续阶段（P8 数据治理 / P10 打包）。
#: 它们**如实报 skipped**，而不是假装成功 ——
#: 一个"成功但什么也没做"的备份任务会让用户以为备份在跑。
_NOT_IMPLEMENTED = {
    "backup": "自动备份尚未实现（P8 数据治理）",
    "report": "报表定时生成尚未实现（P10）",
    "bill": "账单日提醒尚未实现（待接入账单数据）",
    "repayment": "还款日提醒尚未实现（待接入债务数据）",
}


# -----------------------------------------------------------------------------
# 时间解析
# -----------------------------------------------------------------------------
def _parse_at(value: str | None) -> time:
    text = (value or "09:00").strip()
    try:
        hour, minute = text.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError) as error:
        raise ValidationError("时刻格式应为 HH:MM", field="rule.at", got=text) from error


def _month_candidate(session: Session, rule: dict[str, Any], year: int, month: int) -> date | None:
    """某个月的候选日（已按工作日策略调整）。"""
    kind = str(rule.get("day_kind") or "fixed")
    total = monthrange(year, month)[1]
    if kind == "month_end":
        base = date(year, month, total)
    elif kind == "last_workday":
        days = workdays.month_workdays(session, year, month)
        base = days[-1] if days else date(year, month, total)
    elif kind == "nth_workday":
        base = workdays.nth_workday_of_month(session, year, month, int(rule.get("nth") or 1))
        if base is None:
            return None
    else:
        wanted = int(rule.get("day_of_month") or 1)
        base = date(year, month, min(wanted, total))
    if workdays.is_workday(session, base):
        return base
    # 用与发薪规则同一套策略：周末与节假日分别配置
    is_holiday = bool(workdays.is_holiday(session, base))
    policy = str(rule.get("holiday_policy" if is_holiday else "weekend_policy") or "advance")
    shifted, _ = workdays.shift_to_workday(session, base, policy)
    return shifted


def compute_next_run(
    session: Session, task: ScheduledTask, *, after: datetime | None = None
) -> datetime | None:
    """算出下一次应当执行的时刻（严格晚于 `after`）。

    每次执行后重新计算，而不是预先排一批实例：
    **改了规则要立刻生效**，而预排的实例还得去清理。
    """
    rule = task.rule or {}
    frequency = str(rule.get("frequency") or "daily")
    at = _parse_at(rule.get("at"))
    cursor = (after or datetime.now()).replace(second=0, microsecond=0)

    if frequency == "once":
        raw = rule.get("date")
        if not raw:
            return None
        target = datetime.combine(date.fromisoformat(str(raw)), at)
        return target if target > cursor else None

    if frequency == "daily":
        candidate = datetime.combine(cursor.date(), at)
        return candidate if candidate > cursor else candidate + timedelta(days=1)

    if frequency == "weekly":
        weekday = int(rule.get("weekday") or 0)
        candidate = datetime.combine(cursor.date(), at)
        delta = (weekday - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= cursor:
            candidate += timedelta(days=7)
        return candidate

    if frequency != "monthly":
        raise ValidationError(
            "未知的频率", field="rule.frequency", allowed=["daily", "weekly", "monthly", "once"]
        )

    # 月度：从当月开始找，最多往后找 24 个月（防止规则自相矛盾时死循环）
    year, month = cursor.year, cursor.month
    for _ in range(24):
        day = _month_candidate(session, rule, year, month)
        if day is not None:
            candidate = datetime.combine(day, at)
            if candidate > cursor:
                return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1
    return None


# -----------------------------------------------------------------------------
# 任务 CRUD
# -----------------------------------------------------------------------------
def upsert_task(
    session: Session,
    *,
    code: str,
    name: str,
    kind: str = "custom",
    rule: dict[str, Any] | None = None,
    catch_up_policy: str = "startup",
    enabled: bool = True,
    priority: int = 5,
    ref_id: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> ScheduledTask:
    cleaned = (code or "").strip()
    if not cleaned:
        raise ValidationError("任务标识不能为空", field="code")
    if kind not in TASK_KINDS:
        raise ValidationError(f"未知的任务类型：{kind}", field="kind", allowed=list(TASK_KINDS))
    if catch_up_policy not in {"startup", "immediate", "record_only"}:
        raise ValidationError("未知的补办策略", field="catch_up_policy")
    if not 0 <= int(priority) <= 9:
        raise ValidationError("优先级必须在 0–9 之间", field="priority")
    payload = dict(rule or {})
    # 先解析一次时刻，坏规则在保存时就报，而不是等到执行那天
    _parse_at(payload.get("at"))

    task = session.scalar(select(ScheduledTask).where(ScheduledTask.code == cleaned))
    if task is None:
        task = ScheduledTask(code=cleaned)
        session.add(task)
    task.name = (name or cleaned).strip()[:64]
    task.kind = kind
    task.rule = payload
    task.catch_up_policy = catch_up_policy
    task.enabled = bool(enabled)
    task.priority = int(priority)
    task.ref_id = ref_id
    task.note = (note or "")[:200]
    task.next_run_at = compute_next_run(session, task, after=now or datetime.now()) if task.enabled else None
    session.flush()
    return task


def list_tasks(session: Session, *, include_disabled: bool = True) -> list[ScheduledTask]:
    statement = select(ScheduledTask)
    if not include_disabled:
        statement = statement.where(ScheduledTask.enabled.is_(True))
    return list(session.scalars(statement.order_by(ScheduledTask.priority.desc(), ScheduledTask.id)).all())


def get_task(session: Session, task_id: int) -> ScheduledTask:
    row = session.get(ScheduledTask, task_id)
    if row is None:
        raise NotFoundError("任务不存在", entity="scheduled_task", entity_id=task_id)
    return row


def delete_task(session: Session, task_id: int) -> None:
    row = get_task(session, task_id)
    session.delete(row)
    session.flush()


def serialize_task(session: Session, row: ScheduledTask) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "kind": row.kind,
        "rule": row.rule,
        "catch_up_policy": row.catch_up_policy,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "enabled": row.enabled,
        "priority": row.priority,
        "ref_id": row.ref_id,
        "note": row.note,
        # 让界面能直接标出"这个类型还没实现"
        "implemented": row.kind not in _NOT_IMPLEMENTED,
        "not_implemented_reason": _NOT_IMPLEMENTED.get(row.kind, ""),
    }


# -----------------------------------------------------------------------------
# 处理器
# -----------------------------------------------------------------------------
def _handle_payday(session: Session, task: ScheduledTask, moment: datetime) -> dict[str, Any]:
    """发薪日：建（或找到）当期的工资草稿，并按需入队强弹提示。"""
    if task.ref_id is None:
        return {"status": "skipped", "summary": "发薪任务没有关联来源"}
    source = session.get(PaySource, task.ref_id)
    if source is None or source.deleted_at is not None:
        return {"status": "skipped", "summary": "关联的薪资来源已被删除"}

    period = f"{moment.year:04d}-{moment.month:02d}"
    record = payroll_service.ensure_record_for_period(session, source.id, period)
    rule = session.scalar(select(PaydayRule).where(PaydayRule.source_id == source.id))
    if rule is not None and rule.require_form and record.status == "draft":
        create_prompt(
            session,
            kind="payroll",
            title=f"{period} 工资待填写",
            body=f"「{source.name}」本期的工资还没有录入。填完之后实发金额会自动入账。",
            target_kind="payroll_record",
            target_id=record.id,
            blocking_level="strong",
            dedupe_key=f"payroll:{source.id}:{period}",
            max_snooze=3,
            now=moment,
        )
    return {"status": "success", "summary": f"{period} 工资草稿已就绪（#{record.id}）"}


def _handle_insurance(session: Session, task: ScheduledTask, moment: datetime) -> dict[str, Any]:
    if task.ref_id is None:
        return {"status": "skipped", "summary": "五险一金任务没有关联档案"}
    profile = session.get(InsuranceProfile, task.ref_id)
    if profile is None or profile.deleted_at is not None:
        return {"status": "skipped", "summary": "关联的参保档案已被删除"}
    period = f"{moment.year:04d}-{moment.month:02d}"
    computed = insurance_service.compute_contribution(session, profile.id)
    if computed["incomplete"]:
        # **不写一堆 0**：比例没填时写入 0 会让账户余额与年度对账都偏小，
        # 而用户无从察觉。
        #
        # 注意"未填"只统计**启用中**的险种：不适用当地政策的险种
        # （例如没有补充公积金的城市）应当**停用**而不是留空 ——
        # 留空之后系统无法区分"这里不缴"与"我还没填"。

        return {
            "status": "skipped",
            "summary": f"险种比例未填齐，跳过写入（未填：{'、'.join(computed['unfilled_items'][:3])}）",
        }
    rows = insurance_service.record_contribution(session, profile.id, period)
    return {"status": "success", "summary": f"{period} 五险一金已写入 {len(rows)} 条"}


def _handle_recurring(session: Session, task: ScheduledTask, moment: datetime) -> dict[str, Any]:
    """周期记账：交给 P1 的周期规则服务。"""
    from . import recurring as recurring_service

    result = recurring_service.post_due(session, dry_run=False)
    count = len(result) if isinstance(result, list) else int(result or 0)
    return {"status": "success", "summary": f"周期记账投递 {count} 笔"}


def _handle_budget_close(session: Session, task: ScheduledTask, moment: datetime) -> dict[str, Any]:
    """预算月结：预算的"已用"是**算出来的**，因此没有东西需要落库。

    如实说明这一点，而不是记一条"成功"却什么都没做的运行记录。
    """
    from . import budgets as budgets_service

    overview = budgets_service.overview(session, on=moment.date())
    return {
        "status": "success",
        "summary": f"预算为派生值，无需落库（当前 {len(overview.get('items', []))} 条）",
    }


def _handle_custom(session: Session, task: ScheduledTask, moment: datetime) -> dict[str, Any]:
    notify(
        session,
        level="info",
        title=task.name,
        body=task.note or "自定义任务到点了",
        dedupe_key=f"task:{task.code}:{moment.date().isoformat()}",
        now=moment,
    )
    return {"status": "success", "summary": "已生成通知"}


_HANDLERS: dict[str, Callable[[Session, ScheduledTask, datetime], dict[str, Any]]] = {
    "payday": _handle_payday,
    "insurance": _handle_insurance,
    "recurring": _handle_recurring,
    "budget_close": _handle_budget_close,
    "custom": _handle_custom,
}


# -----------------------------------------------------------------------------
# 执行
# -----------------------------------------------------------------------------
def due_tasks(session: Session, *, now: datetime | None = None) -> list[ScheduledTask]:
    moment = now or datetime.now()
    rows = session.scalars(
        select(ScheduledTask).where(
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at.is_not(None),
            ScheduledTask.next_run_at <= moment,
        )
    ).all()
    return sorted(rows, key=lambda row: (row.next_run_at or moment, -row.priority))


def _begin_run_transaction(session: Session) -> None:
    session.flush()
    connection = session.connection()
    # sqlite3 legacy mode does not BEGIN for SELECT/SAVEPOINT. Without this,
    # releasing a handler's savepoint could commit writes before the caller commits.
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def run_due(
    session: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    max_per_task: int = 24,
) -> dict[str, Any]:
    """执行所有到期的任务。

    每个任务最多补办 `max_per_task` 次：软件可能几个月没开，
    而一次性补 365 次会在这里卡住好几秒。上限之外只推进时间并记一条
    `skipped`，让用户知道"漏了很多次"。
    """
    _begin_run_transaction(session)
    if dry_run:
        # 处理器可能创建工资、通知和流水，试运行必须回滚整个工作单元。
        # begin_nested 会先 flush 调用者已有的更改，不会回滚调用者的任务配置。
        with session.begin_nested() as preview:
            result = run_due(session, now=now, max_per_task=max_per_task)
            preview.rollback()
        result["dry_run"] = True
        return result
    moment = now or datetime.now()
    results: list[dict[str, Any]] = []
    for task in due_tasks(session, now=moment):
        runs = 0
        while task.next_run_at is not None and task.next_run_at <= moment and runs < max_per_task:
            scheduled_at = task.next_run_at
            caught_up = (moment - scheduled_at) > timedelta(minutes=30)
            if task.catch_up_policy == "record_only" and caught_up:
                outcome = {"status": "skipped", "summary": "补办策略为仅记录，未执行"}
            elif task.kind in _NOT_IMPLEMENTED:
                outcome = {
                    "status": "skipped",
                    "summary": _NOT_IMPLEMENTED[task.kind],
                }
            else:
                handler = _HANDLERS.get(task.kind)
                if handler is None:
                    outcome = {"status": "skipped", "summary": f"没有 {task.kind} 的处理器"}
                else:
                    try:
                        with session.begin_nested():
                            outcome = handler(session, task, scheduled_at)
                    except Exception as error:  # noqa: BLE001 - 单独一次失败不该中断整轮
                        _logger.warning("任务 %s 执行失败：%s", task.code, error)
                        outcome = {"status": "failed", "summary": "", "error": str(error)[:300]}

            results.append(
                {
                    "task_id": task.id,
                    "code": task.code,
                    "scheduled_at": scheduled_at.isoformat(),
                    "caught_up": caught_up,
                    **outcome,
                }
            )
            if not dry_run:
                _record_run(session, task, scheduled_at, outcome, caught_up, moment)
                task.last_run_at = moment
            runs += 1
            task.next_run_at = compute_next_run(session, task, after=scheduled_at)
            if task.next_run_at is None:
                break
        if runs >= max_per_task and task.next_run_at is not None and task.next_run_at <= moment:
            # 漏得太多：推进到"现在"之后，并明确记一笔
            task.next_run_at = compute_next_run(session, task, after=moment)
            results.append(
                {
                    "task_id": task.id,
                    "code": task.code,
                    "scheduled_at": moment.isoformat(),
                    "caught_up": True,
                    "status": "skipped",
                    "summary": f"漏办超过 {max_per_task} 次，已跳过更早的实例",
                }
            )
    session.flush()
    return {"items": results, "count": len(results), "dry_run": dry_run}


def _record_run(
    session: Session,
    task: ScheduledTask,
    scheduled_at: datetime,
    outcome: dict[str, Any],
    caught_up: bool,
    moment: datetime,
) -> TaskRun:
    run = TaskRun(
        task_id=task.id,
        code=task.code,
        scheduled_at=scheduled_at,
        started_at=moment,
        finished_at=moment,
        status=str(outcome.get("status") or "success"),
        summary=str(outcome.get("summary") or "")[:200],
        error=str(outcome.get("error") or "")[:300],
        caught_up=caught_up,
    )
    session.add(run)
    return run


def run_task_now(
    session: Session, task_id: int, *, now: datetime | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """立即执行一个任务，**不等计划时刻**。

    为什么需要它：`next_run_at` 总是从 `now()` 往后算，
    因此通过界面创建的任务永远是"未来的" —— 没有这个入口，
    用户想手动触发一次发薪流程就做不到（只能等到那天）。
    执行留痕的 `scheduled_at` 用计划时刻，`caught_up` 标出它提前跑了。
    """
    _begin_run_transaction(session)
    if dry_run:
        with session.begin_nested() as preview:
            result = run_task_now(session, task_id, now=now)
            preview.rollback()
        result["dry_run"] = True
        return result
    moment = now or datetime.now()
    task = get_task(session, task_id)
    scheduled_at = task.next_run_at or moment
    if task.kind in _NOT_IMPLEMENTED:
        outcome: dict[str, Any] = {
            "status": "skipped",
            "summary": _NOT_IMPLEMENTED[task.kind],
        }
    else:
        handler = _HANDLERS.get(task.kind)
        if handler is None:
            outcome = {"status": "skipped", "summary": f"没有 {task.kind} 的处理器"}
        else:
            try:
                with session.begin_nested():
                    outcome = handler(session, task, moment)
            except Exception as error:  # noqa: BLE001
                _logger.warning("任务 %s 手动执行失败：%s", task.code, error)
                outcome = {"status": "failed", "summary": "", "error": str(error)[:300]}
    if not dry_run:
        _record_run(session, task, scheduled_at, outcome, True, moment)
        task.last_run_at = moment
    session.flush()
    return {
        "task_id": task.id,
        "code": task.code,
        "scheduled_at": scheduled_at.isoformat(),
        "manual": True,
        "caught_up": True,
        **outcome,
    }


def task_history(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = session.scalars(select(TaskRun).order_by(TaskRun.id.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "task_id": row.task_id,
            "code": row.code,
            "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
            "started_at": row.started_at.isoformat(),
            "status": row.status,
            "summary": row.summary,
            "error": row.error,
            "caught_up": row.caught_up,
        }
        for row in rows
    ]


def health(session: Session) -> dict[str, Any]:
    """任务健康度：成功/跳过/失败各多少。"""
    rows = session.execute(select(TaskRun.status, func.count(TaskRun.id)).group_by(TaskRun.status)).all()
    counts = {str(row[0]): int(row[1]) for row in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "success": counts.get("success", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0),
        "caught_up": int(
            session.scalar(select(func.count(TaskRun.id)).where(TaskRun.caught_up.is_(True))) or 0
        ),
        # 失败率是这里唯一值得报警的指标
        "failure_ratio": (counts.get("failed", 0) / total) if total else None,
    }


# -----------------------------------------------------------------------------
# 待办提示（强弹与漏填拦截）
# -----------------------------------------------------------------------------
def create_prompt(
    session: Session,
    *,
    kind: str,
    title: str,
    body: str = "",
    target_kind: str = "",
    target_id: int | None = None,
    blocking_level: str = "strong",
    dedupe_key: str = "",
    max_snooze: int = 3,
    now: datetime | None = None,
) -> PendingPrompt:
    """入队一个待办。**去重**：同一件事不该反复入队。

    发薪日可能被多次触发（手动执行 + 启动补办），
    没有去重的话用户会看到一屏一模一样的弹窗。
    """
    if blocking_level not in {"strong", "normal"}:
        raise ValidationError("未知的拦截级别", field="blocking_level")
    moment = now or datetime.now()
    if dedupe_key:
        existing = session.scalar(
            select(PendingPrompt).where(
                PendingPrompt.dedupe_key == dedupe_key,
                PendingPrompt.status.in_(["pending", "snoozed"]),
            )
        )
        if existing is not None:
            return existing
    row = PendingPrompt(
        kind=kind,
        title=title[:80],
        body=body[:400],
        blocking_level=blocking_level,
        target_kind=target_kind,
        target_id=target_id,
        max_snooze=max(0, int(max_snooze)),
        dedupe_key=dedupe_key[:80],
        next_remind_at=moment,
    )
    session.add(row)
    session.flush()
    return row


def list_prompts(session: Session, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    statement = select(PendingPrompt)
    if status:
        statement = statement.where(PendingPrompt.status == status)
    rows = session.scalars(statement.order_by(PendingPrompt.id.desc()).limit(limit)).all()
    return [serialize_prompt(row) for row in rows]


def blocking_prompts(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """进入**"必须处理"队列**的提示。

    只有 `blocking_level='strong'` 且状态是 pending/snoozed 且已到提醒时刻的
    才进来。`snoozed` 未到时刻的不该拦住用户 —— 那是"稍后提醒"的字面意思。
    """
    moment = now or datetime.now()
    rows = session.scalars(
        select(PendingPrompt)
        .where(
            PendingPrompt.blocking_level == "strong",
            PendingPrompt.status.in_(["pending", "snoozed"]),
        )
        .order_by(PendingPrompt.id)
    ).all()
    return [
        serialize_prompt(row) for row in rows if row.next_remind_at is None or row.next_remind_at <= moment
    ]


def get_prompt(session: Session, prompt_id: int) -> PendingPrompt:
    row = session.get(PendingPrompt, prompt_id)
    if row is None:
        raise NotFoundError("提示不存在", entity="pending_prompt", entity_id=prompt_id)
    return row


def snooze_prompt(
    session: Session,
    prompt_id: int,
    *,
    minutes: int = 30,
    now: datetime | None = None,
) -> PendingPrompt:
    """稍后提醒。**有次数上限** —— 到顶后只剩"填"或"跳过"两条路。

    没有上限的"稍后"等于"永不"，而拦截也就失去了意义。
    """
    if minutes <= 0:
        raise ValidationError("稍后提醒的间隔必须大于 0", field="minutes")
    moment = now or datetime.now()
    row = get_prompt(session, prompt_id)
    if row.status in {"resolved", "skipped"}:
        raise ConflictError("这条提示已经处理过了", entity="pending_prompt", entity_id=prompt_id)
    if row.snooze_count >= row.max_snooze:
        raise ConflictError(
            f"「稍后提醒」已用完 {row.max_snooze} 次",
            entity="pending_prompt",
            entity_id=prompt_id,
            snooze_count=row.snooze_count,
            suggestion="填写工资，或选择「本月跳过」并说明原因",
        )
    row.snooze_count += 1
    row.status = "snoozed"
    row.next_remind_at = moment + timedelta(minutes=int(minutes))
    session.flush()
    return row


def skip_prompt(
    session: Session, prompt_id: int, *, reason: str, now: datetime | None = None
) -> PendingPrompt:
    """跳过。**必须给出原因并留痕**（可事后补录）。

    这是"合规出口"：只有"必须填"会让真的没工资的月份变成死锁，
    而用户会开始随手填假数据 —— 那比不填更糟。
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        raise ValidationError("跳过必须给出原因", field="reason")
    moment = now or datetime.now()
    row = get_prompt(session, prompt_id)
    if row.status in {"resolved", "skipped"}:
        raise ConflictError("这条提示已经处理过了", entity="pending_prompt", entity_id=prompt_id)
    row.status = "skipped"
    row.skip_reason = cleaned[:200]
    row.resolved_at = moment
    session.flush()
    return row


def resolve_prompt(session: Session, prompt_id: int, *, now: datetime | None = None) -> PendingPrompt:
    """标记为已处理（用户填完了工资表）。"""
    moment = now or datetime.now()
    row = get_prompt(session, prompt_id)
    row.status = "resolved"
    row.resolved_at = moment
    session.flush()
    return row


def resolve_prompts_for_target(
    session: Session, *, target_kind: str, target_id: int, now: datetime | None = None
) -> int:
    """填完工资表之后，把指向它的提示一并了结。

    否则用户填完了还被拦着，而"我已经填过了"是他最自然的反应。
    """
    moment = now or datetime.now()
    rows = session.scalars(
        select(PendingPrompt).where(
            PendingPrompt.target_kind == target_kind,
            PendingPrompt.target_id == target_id,
            PendingPrompt.status.in_(["pending", "snoozed"]),
        )
    ).all()
    for row in rows:
        row.status = "resolved"
        row.resolved_at = moment
    session.flush()
    return len(rows)


def serialize_prompt(row: PendingPrompt) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "blocking_level": row.blocking_level,
        "status": row.status,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "snooze_count": row.snooze_count,
        "max_snooze": row.max_snooze,
        "snooze_left": max(0, row.max_snooze - row.snooze_count),
        "next_remind_at": row.next_remind_at.isoformat() if row.next_remind_at else None,
        "skip_reason": row.skip_reason,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


# -----------------------------------------------------------------------------
# 通知中心
# -----------------------------------------------------------------------------
def notify(
    session: Session,
    *,
    level: str,
    title: str,
    body: str = "",
    action_path: str = "",
    action_label: str = "",
    dedupe_key: str = "",
    now: datetime | None = None,
) -> Notification:
    if level not in {"info", "success", "warn", "error"}:
        raise ValidationError("未知的通知级别", field="level")
    if dedupe_key:
        existing = session.scalar(select(Notification).where(Notification.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
    row = Notification(
        level=level,
        title=title[:80],
        body=body[:400],
        action_path=action_path[:120],
        action_label=action_label[:32],
        dedupe_key=dedupe_key[:80],
    )
    session.add(row)
    session.flush()
    return row


def list_notifications(
    session: Session, *, unread_only: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    statement = select(Notification)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows = session.scalars(statement.order_by(Notification.id.desc()).limit(limit)).all()
    return [serialize_notification(row) for row in rows]


def unread_count(session: Session) -> int:
    return int(session.scalar(select(func.count(Notification.id)).where(Notification.read_at.is_(None))) or 0)


def mark_read(session: Session, notification_id: int, *, now: datetime | None = None) -> None:
    row = session.get(Notification, notification_id)
    if row is None:
        raise NotFoundError("通知不存在", entity="notification", entity_id=notification_id)
    row.read_at = now or datetime.now()
    session.flush()


def mark_all_read(session: Session, *, now: datetime | None = None) -> int:
    moment = now or datetime.now()
    rows = session.scalars(select(Notification).where(Notification.read_at.is_(None))).all()
    for row in rows:
        row.read_at = moment
    session.flush()
    return len(rows)


def serialize_notification(row: Notification) -> dict[str, Any]:
    return {
        "id": row.id,
        "level": row.level,
        "title": row.title,
        "body": row.body,
        "action_path": row.action_path,
        "action_label": row.action_label,
        "read": row.read_at is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
