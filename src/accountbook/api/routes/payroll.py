"""P6 路由：薪酬、五险一金、定时任务、提醒与通知。

**字面量路径一律声明在参数路径之前**：FastAPI 按声明顺序匹配，
P5 时 `/api/ledger/{account_id}` 在前导致 `/api/ledger/trial-balance`
被当成账号、永远 422。这里分批组织，避免同类问题。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from ...core.errors import ValidationError
from ...services import insurance as insurance_service
from ...services import payroll as payroll_service
from ...services import scheduler as scheduler_service
from ...services import workdays
from ..deps import get_session
from ..schemas import (
    InsuranceContributionIn,
    InsuranceItemIn,
    InsuranceProfileIn,
    InsuranceProfilePatch,
    InsuranceStatementIn,
    InsuranceWithdrawalIn,
    PayComponentIn,
    PayComponentPatch,
    PaydayRuleIn,
    PayrollFillIn,
    PayrollRecordIn,
    PayrollSkipIn,
    PaySourceIn,
    PaySourcePatch,
    ScheduledTaskIn,
    SkipIn,
    SnoozeIn,
    WorkdayImportIn,
    WorkdayOverrideIn,
)
from ..state import context_of

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["payroll"])

SessionDep = Depends(get_session)


# =============================================================================
# 薪酬：来源
# =============================================================================
@router.get("/payroll/sources", summary="薪资来源列表")
def list_sources(include_disabled: bool = Query(default=True), session: Session = SessionDep) -> Any:
    rows = payroll_service.list_sources(session, include_disabled=include_disabled)
    return {
        "items": [payroll_service.serialize_source(row) for row in rows],
        "count": len(rows),
    }


@router.post("/payroll/sources", status_code=status.HTTP_201_CREATED, summary="新建薪资来源")
def create_source(payload: PaySourceIn, session: Session = SessionDep) -> Any:
    row = payroll_service.add_source(session, **payload.model_dump())
    return payroll_service.serialize_source(row)


@router.patch("/payroll/sources/{source_id}", summary="修改薪资来源")
def update_source(source_id: int, payload: PaySourcePatch, session: Session = SessionDep) -> Any:
    changes = payload.model_dump(exclude_unset=True)
    row = payroll_service.update_source(session, source_id, **changes)
    return payroll_service.serialize_source(row)


@router.delete(
    "/payroll/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除薪资来源",
)
def delete_source(source_id: int, session: Session = SessionDep) -> None:
    payroll_service.delete_source(session, source_id)


@router.post("/payroll/sources/{source_id}/restore", summary="恢复薪资来源")
def restore_source(source_id: int, session: Session = SessionDep) -> Any:
    return payroll_service.serialize_source(payroll_service.restore_source(session, source_id))


@router.get("/payroll/sources/{source_id}/rule", summary="读取发薪规则")
def get_payday_rule(source_id: int, session: Session = SessionDep) -> Any:
    return payroll_service.serialize_rule(payroll_service.get_payday_rule(session, source_id))


@router.put("/payroll/sources/{source_id}/rule", summary="设置发薪规则")
def upsert_payday_rule(source_id: int, payload: PaydayRuleIn, session: Session = SessionDep) -> Any:
    rule = payroll_service.upsert_payday_rule(session, source_id, **payload.model_dump())
    return payroll_service.serialize_rule(rule)


# =============================================================================
# 薪酬：组成项
# =============================================================================
@router.get("/payroll/components", summary="组成项列表")
def list_components(
    source_id: int | None = Query(default=None),
    include_disabled: bool = Query(default=True),
    session: Session = SessionDep,
) -> Any:
    rows = payroll_service.list_components(session, source_id=source_id, include_disabled=include_disabled)
    return {
        "items": [payroll_service.serialize_component(row) for row in rows],
        "count": len(rows),
    }


@router.post("/payroll/components", status_code=status.HTTP_201_CREATED, summary="新建组成项")
def create_component(payload: PayComponentIn, session: Session = SessionDep) -> Any:
    row = payroll_service.create_component(session, **payload.model_dump())
    return payroll_service.serialize_component(row)


@router.patch("/payroll/components/{component_id}", summary="修改组成项")
def update_component(component_id: int, payload: PayComponentPatch, session: Session = SessionDep) -> Any:
    changes = payload.model_dump(exclude_unset=True)
    row = payroll_service.update_component(session, component_id, **changes)
    return payroll_service.serialize_component(row)


@router.delete(
    "/payroll/components/{component_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除组成项",
)
def delete_component(component_id: int, session: Session = SessionDep) -> None:
    payroll_service.delete_component(session, component_id)


# =============================================================================
# 薪酬：收录记录与总览
#   注意顺序：`/records/{record_id}` 之前先声明它没有字面量冲突的兄弟；
#   `fill/skip/recompute` 都是 `/{id}/xxx` 形式，不会与 `/{id}` 混淆
# =============================================================================
@router.get("/payroll/records", summary="工资记录列表")
def list_records(
    source_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    start_period: str | None = Query(default=None),
    end_period: str | None = Query(default=None),
    limit: int = Query(default=240, ge=1, le=1000),
    session: Session = SessionDep,
) -> Any:
    rows = payroll_service.list_records(
        session,
        source_id=source_id,
        status=status_filter,
        start_period=start_period,
        end_period=end_period,
        limit=limit,
    )
    return {
        "items": [payroll_service.serialize_record(row) for row in rows],
        "count": len(rows),
    }


@router.post("/payroll/records", status_code=status.HTTP_201_CREATED, summary="新建工资记录")
def create_record(payload: PayrollRecordIn, session: Session = SessionDep) -> Any:
    row = payroll_service.create_record(session, **payload.model_dump())
    return payroll_service.serialize_record(row)


@router.get("/payroll/pay-date", summary="某来源某期间的发薪日")
def pay_date(
    source_id: int = Query(...),
    period: str = Query(...),
    session: Session = SessionDep,
) -> Any:
    """带 `confidence`：`exact`（有规则且日历数据齐全）/ `assumed`
    （有规则但该年节假日未录入）/ `inferred`（还没配规则，用的是默认 15 日）。

    界面据此决定要不要提示"这个日期可能不准"。
    """
    return payroll_service.pay_date_for(session, source_id, period)


@router.get("/payroll/upcoming", summary="未来发薪日")
def upcoming(months: int = Query(default=3, ge=1, le=24), session: Session = SessionDep) -> Any:
    return {"items": payroll_service.upcoming(session, months=months)}


@router.get("/payroll/pending", summary="待填写的工资记录（已过宽限期）")
def pending(session: Session = SessionDep) -> Any:
    return {"items": payroll_service.pending_records(session)}


@router.get("/payroll/overview", summary="薪酬总览（含同比）")
def overview(
    period: str = Query(...),
    history_months: int = Query(default=12, ge=1, le=60),
    session: Session = SessionDep,
) -> Any:
    return payroll_service.payroll_overview(session, period=period, history_months=history_months)


@router.get("/payroll/compute", summary="试算某来源的工资（不落库）")
def compute(source_id: int = Query(...), session: Session = SessionDep) -> Any:
    """**不落库**：改组成项时界面需要能立刻看到新结果，
    而"先执行再看结果"对工资表是不可接受的。"""
    return payroll_service.compute_payroll(session, source_id)


@router.get("/payroll/records/{record_id}", summary="工资记录详情")
def get_record(record_id: int, session: Session = SessionDep) -> Any:
    return payroll_service.serialize_record(payroll_service.get_record(session, record_id))


@router.post("/payroll/records/{record_id}/recompute", summary="按当前模板重算（草稿）")
def recompute_record(record_id: int, session: Session = SessionDep) -> Any:
    return payroll_service.serialize_record(payroll_service.recompute_record(session, record_id))


@router.post("/payroll/records/{record_id}/fill", summary="填写完成并（可选）入账")
def fill_record(record_id: int, payload: PayrollFillIn, session: Session = SessionDep) -> Any:
    result = payroll_service.fill_record(session, record_id, **payload.model_dump())
    # 填完之后把指向它的强弹一并了结 —— 否则用户填完了还被拦着
    scheduler_service.resolve_prompts_for_target(session, target_kind="payroll_record", target_id=record_id)
    return result


@router.post("/payroll/records/{record_id}/skip", summary="本月跳过（必须给原因）")
def skip_record(record_id: int, payload: PayrollSkipIn, session: Session = SessionDep) -> Any:
    """**合规出口。** 只有"必须填"会让真的没工资的月份变成死锁，
    而用户会开始随手填假数据 —— 那比不填更糟。"""
    row = payroll_service.skip_record(session, record_id, reason=payload.reason)
    scheduler_service.resolve_prompts_for_target(session, target_kind="payroll_record", target_id=record_id)
    return payroll_service.serialize_record(row)


@router.delete(
    "/payroll/records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除工资记录（已入账的不可删）",
)
def delete_record(record_id: int, session: Session = SessionDep) -> None:
    payroll_service.delete_record(session, record_id)


# =============================================================================
# 五险一金
# =============================================================================
@router.get("/insurance/items", summary="险种字典")
def list_items(
    city: str | None = Query(default=None),
    include_disabled: bool = Query(default=True),
    session: Session = SessionDep,
) -> Any:
    rows = insurance_service.list_items(session, city=city, include_disabled=include_disabled)
    return {
        "items": [insurance_service.serialize_item(row) for row in rows],
        "count": len(rows),
    }


@router.post("/insurance/items/ensure", summary="铺标准险种（幂等，不覆盖已填比例）")
def ensure_items(city: str = Query(default=""), session: Session = SessionDep) -> Any:
    """只铺**名称与结构标志**，比例一律留 0 ——
    比例因城市与年份而异，写死等于给一个看起来权威、实际只对某市某年成立的数字。
    **不覆盖用户已填的比例**，否则填完再触发一次初始化就把比例清掉了。
    """
    rows = insurance_service.ensure_standard_items(session, city=city)
    return {
        "items": [insurance_service.serialize_item(row) for row in rows],
        "count": len(rows),
    }


@router.put("/insurance/items", summary="新建或更新险种（按 kind + city）")
def upsert_item(payload: InsuranceItemIn, session: Session = SessionDep) -> Any:
    changes = payload.model_dump(exclude_unset=True)
    kind = changes.pop("kind")
    city = changes.pop("city", "")
    row = insurance_service.upsert_item(session, kind=kind, city=city, **changes)
    return insurance_service.serialize_item(row)


@router.delete(
    "/insurance/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除险种（已有缴纳记录的不可删）",
)
def delete_item(item_id: int, session: Session = SessionDep) -> None:
    insurance_service.delete_item(session, item_id)


@router.get("/insurance/profiles", summary="参保档案列表")
def list_profiles(include_disabled: bool = Query(default=False), session: Session = SessionDep) -> Any:
    rows = insurance_service.list_profiles(session, include_disabled=include_disabled)
    return {
        "items": [insurance_service.serialize_profile(session, row) for row in rows],
        "count": len(rows),
    }


@router.post("/insurance/profiles", status_code=status.HTTP_201_CREATED, summary="新建参保档案")
def create_profile(payload: InsuranceProfileIn, session: Session = SessionDep) -> Any:
    row = insurance_service.add_profile(session, **payload.model_dump())
    return insurance_service.serialize_profile(session, row)


@router.patch("/insurance/profiles/{profile_id}", summary="修改参保档案")
def update_profile(profile_id: int, payload: InsuranceProfilePatch, session: Session = SessionDep) -> Any:
    changes = payload.model_dump(exclude_unset=True)
    row = insurance_service.update_profile(session, profile_id, **changes)
    return insurance_service.serialize_profile(session, row)


@router.delete(
    "/insurance/profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除参保档案",
)
def delete_profile(profile_id: int, session: Session = SessionDep) -> None:
    insurance_service.delete_profile(session, profile_id)


@router.post("/insurance/profiles/{profile_id}/restore", summary="恢复参保档案")
def restore_profile(profile_id: int, session: Session = SessionDep) -> Any:
    row = insurance_service.restore_profile(session, profile_id)
    return insurance_service.serialize_profile(session, row)


@router.get("/insurance/contributions", summary="缴纳记录")
def list_contributions(
    profile_id: int | None = Query(default=None),
    period: str | None = Query(default=None),
    start_period: str | None = Query(default=None),
    end_period: str | None = Query(default=None),
    session: Session = SessionDep,
) -> Any:
    rows = insurance_service.list_contributions(
        session,
        profile_id=profile_id,
        period=period,
        start_period=start_period,
        end_period=end_period,
    )
    return {
        "items": [insurance_service.serialize_contribution(session, row) for row in rows],
        "count": len(rows),
    }


@router.post(
    "/insurance/contributions",
    status_code=status.HTTP_201_CREATED,
    summary="写入某期缴纳（默认幂等）",
)
def record_contribution(payload: InsuranceContributionIn, session: Session = SessionDep) -> Any:
    rows = insurance_service.record_contribution(session, **payload.model_dump())
    return {
        "items": [insurance_service.serialize_contribution(session, row) for row in rows],
        "count": len(rows),
    }


@router.get("/insurance/compute", summary="试算某档案的缴纳额（不落库）")
def compute_contribution(profile_id: int = Query(...), session: Session = SessionDep) -> Any:
    """带 `incomplete` 与 `unfilled_items`：**比例没填齐时合计必然偏小**，
    界面必须能把它显示成"待配置"而不是"缴得少"。"""
    return insurance_service.compute_contribution(session, profile_id)


@router.get("/insurance/accounts", summary="个人账户余额（派生）")
def account_balances(profile_id: int | None = Query(default=None), session: Session = SessionDep) -> Any:
    balances = insurance_service.account_balances(session, profile_id=profile_id)
    return {"balances": balances, "total_minor": sum(balances.values())}


@router.get("/insurance/withdrawals", summary="提取记录")
def list_withdrawals(profile_id: int | None = Query(default=None), session: Session = SessionDep) -> Any:
    rows = insurance_service.list_withdrawals(session, profile_id=profile_id)
    return {
        "items": [insurance_service.serialize_withdrawal(session, row) for row in rows],
        "count": len(rows),
    }


@router.post(
    "/insurance/withdrawals",
    status_code=status.HTTP_201_CREATED,
    summary="新增提取",
)
def add_withdrawal(payload: InsuranceWithdrawalIn, session: Session = SessionDep) -> Any:
    row = insurance_service.add_withdrawal(session, **payload.model_dump())
    return insurance_service.serialize_withdrawal(session, row)


@router.delete(
    "/insurance/withdrawals/{withdrawal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除提取",
)
def delete_withdrawal(withdrawal_id: int, session: Session = SessionDep) -> None:
    insurance_service.delete_withdrawal(session, withdrawal_id)


@router.get("/insurance/statement", summary="年度对账")
def annual_statement(
    profile_id: int = Query(...),
    year: int = Query(...),
    session: Session = SessionDep,
) -> Any:
    """给出系统算出的合计、官方对账单上的数、以及**差异**。

    差异是这份报表的全部意义：它要么证明记录是准的，
    要么指出"有几个月没记 / 有几个月记重了"。
    """
    return insurance_service.annual_statement(session, profile_id, year)


@router.put("/insurance/statement", summary="录入年度对账数与利息")
def upsert_statement(payload: InsuranceStatementIn, session: Session = SessionDep) -> Any:
    insurance_service.upsert_statement(session, **payload.model_dump())
    return insurance_service.annual_statement(session, payload.profile_id, payload.year)


@router.get("/insurance/overview", summary="五险一金总览")
def insurance_overview(
    start_period: str = Query(...),
    end_period: str = Query(...),
    session: Session = SessionDep,
) -> Any:
    return insurance_service.insurance_overview(session, start_period=start_period, end_period=end_period)


# =============================================================================
# 工作日日历
# =============================================================================
@router.get("/workdays", summary="工作日覆盖列表")
def list_workdays(year: int | None = Query(default=None), session: Session = SessionDep) -> Any:
    rows = workdays.list_overrides(session, year=year)
    return {
        "items": [
            {
                "day": row.day.isoformat(),
                "is_workday": row.is_workday,
                "kind": row.kind,
                "name": row.name,
                "source": row.source,
                "note": row.note,
            }
            for row in rows
        ],
        "count": len(rows),
        # 哪些年份已录入 —— 界面据此提示"今年的节假日还没录"
        "covered_years": sorted(workdays.coverage_years(session)),
    }


@router.put("/workdays", summary="覆盖某一天")
def upsert_workday(payload: WorkdayOverrideIn, session: Session = SessionDep) -> Any:
    row = workdays.upsert_override(
        session,
        payload.day,
        is_workday=payload.is_workday,
        name=payload.name,
        note=payload.note,
        source="user_edit",
    )
    return {
        "day": row.day.isoformat(),
        "is_workday": row.is_workday,
        "kind": row.kind,
        "name": row.name,
    }


@router.delete("/workdays/{day}", status_code=status.HTTP_204_NO_CONTENT, summary="取消某天的覆盖")
def delete_workday(day: date, session: Session = SessionDep) -> None:
    workdays.delete_override(session, day)


@router.post("/workdays/import", summary="批量粘贴节假日安排")
def import_workdays(payload: WorkdayImportIn, session: Session = SessionDep) -> Any:
    """解析用户粘贴的官方公告。

    **无法解析的行原样返回**：他粘的是官方公告，如果有一行没被认出来
    必须自己知道 —— 否则那个假期会悄悄变成工作日。
    """
    return workdays.import_workday_text(session, payload.text)


# =============================================================================
# 定时任务
#   注意顺序：`/tasks/due` 这类字面量必须在 `/tasks/{task_id}` 之前
# =============================================================================
@router.get("/scheduler/tasks", summary="任务列表")
def list_tasks(include_disabled: bool = Query(default=True), session: Session = SessionDep) -> Any:
    rows = scheduler_service.list_tasks(session, include_disabled=include_disabled)
    return {
        "items": [scheduler_service.serialize_task(session, row) for row in rows],
        "count": len(rows),
    }


@router.get("/scheduler/tasks/due", summary="到期任务（只看不写）")
def due_tasks(session: Session = SessionDep) -> Any:
    rows = scheduler_service.due_tasks(session)
    return {
        "items": [scheduler_service.serialize_task(session, row) for row in rows],
        "count": len(rows),
    }


@router.post("/scheduler/tasks", summary="新建或更新任务（按 code）")
def upsert_task(payload: ScheduledTaskIn, session: Session = SessionDep) -> Any:
    row = scheduler_service.upsert_task(session, **payload.model_dump())
    return scheduler_service.serialize_task(session, row)


@router.delete(
    "/scheduler/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除任务",
)
def delete_task(task_id: int, session: Session = SessionDep) -> None:
    scheduler_service.delete_task(session, task_id)


@router.post("/scheduler/tasks/{task_id}/run", summary="立即执行某个任务")
def run_task(
    task_id: int,
    request: Request,
    dry_run: bool = Query(default=False),
    session: Session = SessionDep,
) -> Any:
    """不等计划时刻。有了它，用户才能手动触发一次发薪流程并看到强弹。"""
    with context_of(request.app).scheduler_lock:
        result = scheduler_service.run_task_now(session, task_id, dry_run=dry_run)
        session.commit()
        return result


@router.post("/scheduler/run", summary="执行到期任务")
def run_tasks(request: Request, dry_run: bool = Query(default=False), session: Session = SessionDep) -> Any:
    """`dry_run=True` 时只算不写 —— 界面需要能预览"现在点下去会发生什么"。"""
    with context_of(request.app).scheduler_lock:
        result = scheduler_service.run_due(session, dry_run=dry_run)
        session.commit()
        return result


@router.get("/scheduler/history", summary="执行历史")
def task_history(limit: int = Query(default=100, ge=1, le=500), session: Session = SessionDep) -> Any:
    return {"items": scheduler_service.task_history(session, limit=limit)}


@router.get("/scheduler/health", summary="任务健康度")
def task_health(session: Session = SessionDep) -> Any:
    return scheduler_service.health(session)


# -----------------------------------------------------------------------------
# 提醒（强弹与合规出口）
# -----------------------------------------------------------------------------
@router.get("/scheduler/blocking", summary="必须处理的提示（前端门禁）")
def blocking(session: Session = SessionDep) -> Any:
    """只有 `strong` 且已到提醒时刻的才进来。

    前端在进入主界面前拉这个列表；**它必须有出口**，
    否则真的没工资的月份会把用户锁在外面。
    """
    items = scheduler_service.blocking_prompts(session)
    return {"items": items, "count": len(items)}


@router.get("/scheduler/prompts", summary="提示列表")
def list_prompts(
    status_filter: str | None = Query(default=None, alias="status"),
    session: Session = SessionDep,
) -> Any:
    return {"items": scheduler_service.list_prompts(session, status=status_filter)}


@router.get('/reminders', summary='跨页面可处理待办')
def pending_reminders(session: Session = SessionDep):
    from ...services import reminders
    items = reminders.pending(session)
    return {'items': items, 'count': len(items)}


@router.post("/scheduler/prompts/{prompt_id}/snooze", summary="稍后提醒（有次数上限）")
def snooze(prompt_id: int, payload: SnoozeIn, session: Session = SessionDep) -> Any:
    row = scheduler_service.snooze_prompt(session, prompt_id, minutes=payload.minutes)
    return scheduler_service.serialize_prompt(row)


@router.post("/scheduler/prompts/{prompt_id}/skip", summary="跳过（必须给原因）")
def skip(prompt_id: int, payload: SkipIn, session: Session = SessionDep) -> Any:
    row = scheduler_service.skip_prompt(session, prompt_id, reason=payload.reason)
    return scheduler_service.serialize_prompt(row)


@router.post("/scheduler/prompts/{prompt_id}/resolve", summary="标记已处理")
def resolve(prompt_id: int, session: Session = SessionDep) -> Any:
    return scheduler_service.serialize_prompt(scheduler_service.resolve_prompt(session, prompt_id))


# -----------------------------------------------------------------------------
# 通知中心
# -----------------------------------------------------------------------------
@router.get("/notifications", summary="通知列表")
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = SessionDep,
) -> Any:
    items = scheduler_service.list_notifications(session, unread_only=unread_only, limit=limit)
    return {"items": items, "count": len(items), "unread": scheduler_service.unread_count(session)}


@router.get("/notifications/unread-count", summary="未读数")
def unread_count(session: Session = SessionDep) -> Any:
    return {"unread": scheduler_service.unread_count(session)}


@router.post("/notifications/read-all", summary="全部已读")
def read_all(session: Session = SessionDep) -> Any:
    return {"marked": scheduler_service.mark_all_read(session)}


@router.post("/notifications/{notification_id}/read", summary="标记已读")
def read_one(notification_id: int, session: Session = SessionDep) -> Any:
    scheduler_service.mark_read(session, notification_id)
    return {"unread": scheduler_service.unread_count(session)}


def _ensure_positive(value: int, field: str) -> int:
    if value <= 0:
        raise ValidationError("必须是正数", field=field, got=value)
    return value
