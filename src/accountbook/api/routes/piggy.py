"""存钱罐与储蓄目标的 API（P4）。

路由层只做三件事：解析请求、调用服务、序列化响应。
所有校验与业务规则都在 `services/piggy.py` 里 —— 那些规则需要能被
测试直接调用（不经过 HTTP），而路由层重复一遍校验必然产生两套口径。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...core.errors import ValidationError
from ...services import piggy as piggy_service
from ..deps import get_session
from ..schemas import (
    BankDetailOut,
    BankListOut,
    BankOut,
    DepositListOut,
    DepositOut,
    GoalContributionsOut,
    GoalDetailOut,
    GoalListOut,
    GoalOut,
    PiggyActionOut,
    PiggyBankCreate,
    PiggyBankUpdate,
    PiggyContributeRequest,
    PiggyDepositRequest,
    PiggyGoalCreate,
    PiggyGoalUpdate,
    PiggyInjectRequest,
    PiggyRuleRequest,
    PiggyRuleRunRequest,
    PiggySettleRequest,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["piggy"])

SessionDep = Depends(get_session)


# -----------------------------------------------------------------------------
# 存钱罐
# -----------------------------------------------------------------------------
@router.get("/piggy/banks", response_model=BankListOut, summary="存钱罐列表")
def list_banks(
    status_filter: str | None = Query(default=None, alias="status"),
    include_deleted: bool = Query(default=False),
    session: Session = SessionDep,
) -> Any:
    """列表。

    余额**一次查出**（`_sum_all_balances`）：为每个罐子画液面时逐个查
    会变成 N 次往返，而它们的形状完全一样。
    """
    banks = piggy_service.list_banks(session, status=status_filter, include_deleted=include_deleted)
    balances = piggy_service._sum_all_balances(session, [bank.id for bank in banks])
    items = [piggy_service.serialize_bank(session, bank, balance_minor=balances[bank.id]) for bank in banks]
    total_minor = sum(int(item["balance_minor"]) for item in items)
    return {
        "items": items,
        "count": len(items),
        # 目标合计只统计"进行中"的：把已达成的也算进去会让"还差多少"永远不变
        "active_target_minor": sum(
            int(item["target_amount_minor"]) for item in items if item["status"] == "active"
        ),
        "saved_minor": total_minor,
    }


@router.post(
    "/piggy/banks",
    response_model=BankOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建存钱罐",
)
def create_bank(payload: PiggyBankCreate, session: Session = SessionDep) -> Any:
    bank = piggy_service.create_bank(session, **payload.model_dump())
    return piggy_service.serialize_bank(session, bank)


@router.get("/piggy/banks/{bank_id}", response_model=BankDetailOut, summary="存钱罐详情")
def bank_detail(bank_id: int, session: Session = SessionDep) -> Any:
    return piggy_service.bank_detail(session, bank_id)


@router.patch("/piggy/banks/{bank_id}", response_model=BankOut, summary="修改存钱罐")
def update_bank(bank_id: int, payload: PiggyBankUpdate, session: Session = SessionDep) -> Any:
    # 只应用显式给出的字段：`model_dump(exclude_unset=True)` 让
    # "没传" 与 "传了 null" 保持区分，与批量编辑同一套约定
    changes = payload.model_dump(exclude_unset=True)
    bank = piggy_service.update_bank(session, bank_id, **changes)
    return piggy_service.serialize_bank(session, bank)


@router.delete("/piggy/banks/{bank_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除存钱罐")
def delete_bank(bank_id: int, session: Session = SessionDep) -> None:
    piggy_service.delete_bank(session, bank_id)


@router.post("/piggy/banks/{bank_id}/restore", response_model=BankOut, summary="恢复存钱罐")
def restore_bank(bank_id: int, session: Session = SessionDep) -> Any:
    bank = piggy_service.restore_bank(session, bank_id)
    return piggy_service.serialize_bank(session, bank)


# -----------------------------------------------------------------------------
# 存入 / 取出
# -----------------------------------------------------------------------------
@router.get("/piggy/banks/{bank_id}/deposits", response_model=DepositListOut, summary="存入记录")
def list_deposits(
    bank_id: int,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = SessionDep,
) -> Any:
    piggy_service.get_bank(session, bank_id)
    rows = piggy_service.list_deposits(session, bank_id, limit=limit)
    return {
        "items": [piggy_service.serialize_deposit(row) for row in rows],
        "count": len(rows),
        "balance_minor": piggy_service.balance(session, bank_id),
    }


@router.post(
    "/piggy/banks/{bank_id}/deposits",
    response_model=DepositOut,
    status_code=status.HTTP_201_CREATED,
    summary="存入或取出",
)
def add_deposit(bank_id: int, payload: PiggyDepositRequest, session: Session = SessionDep) -> Any:
    row = piggy_service.add_deposit(session, bank_id, **payload.model_dump())
    return piggy_service.serialize_deposit(row)


@router.delete(
    "/piggy/deposits/{deposit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一笔存入记录",
)
def delete_deposit(deposit_id: int, session: Session = SessionDep) -> None:
    piggy_service.delete_deposit(session, deposit_id)


# -----------------------------------------------------------------------------
# 归集规则
# -----------------------------------------------------------------------------
@router.put("/piggy/banks/{bank_id}/rule", response_model=BankOut, summary="设置归集规则")
def upsert_rule(bank_id: int, payload: PiggyRuleRequest, session: Session = SessionDep) -> Any:
    piggy_service.upsert_rule(session, bank_id, **payload.model_dump())
    return piggy_service.serialize_bank(session, piggy_service.get_bank(session, bank_id))


@router.delete(
    "/piggy/banks/{bank_id}/rule",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除归集规则",
)
def delete_rule(bank_id: int, session: Session = SessionDep) -> None:
    piggy_service.delete_rule(session, bank_id)


@router.get("/piggy/rules/due", summary="到期的时间驱动规则（预览）")
def due_rules(today: date | None = Query(default=None), session: Session = SessionDep) -> Any:
    """只看不写：界面需要能预览"今天该攒多少"。

    对自动扣钱的功能来说，"先执行再看结果"是不可接受的。
    """
    return {"items": piggy_service.due_rules(session, today=today)}


@router.post("/piggy/rules/run", response_model=PiggyActionOut, summary="执行到期规则")
def run_rules(payload: PiggyRuleRunRequest, session: Session = SessionDep) -> Any:
    applied = piggy_service.run_due_rules(session, today=payload.today, dry_run=payload.dry_run)
    return {
        "items": applied,
        "count": len(applied),
        "total_minor": sum(int(item["amount_minor"]) for item in applied),
        "dry_run": payload.dry_run,
    }


@router.post("/piggy/rules/apply", response_model=PiggyActionOut, summary="对流水应用事件驱动规则")
def apply_rules(
    transaction_ids: list[int],
    dry_run: bool = Query(default=False),
    session: Session = SessionDep,
) -> Any:
    """四舍五入 / 收入百分比 / 分类触发。

    记完一笔账之后由前端调用；幂等由服务层保证（见其 docstring），
    因此重复调用是安全的。
    """
    if not transaction_ids:
        raise ValidationError("必须给出至少一笔流水", field="transaction_ids")
    applied = piggy_service.apply_rules_to_transactions(session, transaction_ids, dry_run=dry_run)
    return {
        "items": applied,
        "count": len(applied),
        "total_minor": sum(int(item["amount_minor"]) for item in applied),
        "dry_run": dry_run,
    }


# -----------------------------------------------------------------------------
# 达成 / 注入目标
# -----------------------------------------------------------------------------
@router.post("/piggy/banks/{bank_id}/achieve", response_model=BankOut, summary="达成后结清")
def achieve_bank(bank_id: int, payload: PiggySettleRequest, session: Session = SessionDep) -> Any:
    """生成"购买支出"流水并结清罐子。"""
    result = piggy_service.achieve_bank(session, bank_id, **payload.model_dump())
    return {**result["bank"], "settled_minor": result["settled_minor"]}


@router.post("/piggy/banks/{bank_id}/inject", response_model=BankOut, summary="注入储蓄目标")
def inject_bank(bank_id: int, payload: PiggyInjectRequest, session: Session = SessionDep) -> Any:
    result = piggy_service.inject_bank_into_goal(session, bank_id, **payload.model_dump())
    return {**result["bank"], "injected_minor": result["injected_minor"]}


# -----------------------------------------------------------------------------
# 储蓄目标
# -----------------------------------------------------------------------------
@router.get("/goals", response_model=GoalListOut, summary="储蓄目标列表")
def list_goals(
    status_filter: str | None = Query(default=None, alias="status"),
    include_deleted: bool = Query(default=False),
    session: Session = SessionDep,
) -> Any:
    goals = piggy_service.list_goals(session, status=status_filter, include_deleted=include_deleted)
    items = [piggy_service.serialize_goal(session, goal) for goal in goals]
    return {
        "items": items,
        "count": len(items),
        "active_target_minor": sum(
            int(item["target_amount_minor"]) for item in items if item["status"] == "active"
        ),
        "saved_minor": sum(int(item["saved_minor"]) for item in items),
    }


@router.post("/goals", response_model=GoalOut, status_code=status.HTTP_201_CREATED, summary="新建目标")
def create_goal(payload: PiggyGoalCreate, session: Session = SessionDep) -> Any:
    goal = piggy_service.add_goal(session, **payload.model_dump())
    return piggy_service.serialize_goal(session, goal)


@router.get("/goals/{goal_id}", response_model=GoalDetailOut, summary="目标详情")
def goal_detail(goal_id: int, session: Session = SessionDep) -> Any:
    goal = piggy_service.get_goal(session, goal_id)
    rows = piggy_service.list_contributions(session, goal_id)
    return {
        **piggy_service.serialize_goal(session, goal),
        "contributions": [
            {
                "id": row.id,
                "goal_id": row.goal_id,
                "amount_minor": row.amount_minor,
                "occurred_at": row.occurred_at.isoformat(),
                "tz_offset_minutes": row.tz_offset_minutes,
                "note": row.note,
            }
            for row in rows
        ],
    }


@router.patch("/goals/{goal_id}", response_model=GoalOut, summary="修改目标")
def update_goal(goal_id: int, payload: PiggyGoalUpdate, session: Session = SessionDep) -> Any:
    changes = payload.model_dump(exclude_unset=True)
    goal = piggy_service.update_goal(session, goal_id, **changes)
    return piggy_service.serialize_goal(session, goal)


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除目标")
def delete_goal(goal_id: int, session: Session = SessionDep) -> None:
    piggy_service.delete_goal(session, goal_id)


@router.post("/goals/{goal_id}/restore", response_model=GoalOut, summary="恢复目标")
def restore_goal(goal_id: int, session: Session = SessionDep) -> Any:
    goal = piggy_service.restore_goal(session, goal_id)
    return piggy_service.serialize_goal(session, goal)


@router.get("/goals/{goal_id}/contributions", response_model=GoalContributionsOut, summary="注入记录")
def list_contributions(
    goal_id: int,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = SessionDep,
) -> Any:
    goal = piggy_service.get_goal(session, goal_id)
    rows = piggy_service.list_contributions(session, goal_id, limit=limit)
    return {
        "items": [
            {
                "id": row.id,
                "goal_id": row.goal_id,
                "amount_minor": row.amount_minor,
                "occurred_at": row.occurred_at.isoformat(),
                "tz_offset_minutes": row.tz_offset_minutes,
                "note": row.note,
            }
            for row in rows
        ],
        "count": len(rows),
        "saved_minor": piggy_service.goal_saved(session, goal),
    }


@router.post(
    "/goals/{goal_id}/contributions",
    response_model=GoalContributionsOut,
    status_code=status.HTTP_201_CREATED,
    summary="注入一笔",
)
def contribute(goal_id: int, payload: PiggyContributeRequest, session: Session = SessionDep) -> Any:
    row = piggy_service.contribute(session, goal_id, **payload.model_dump())
    goal = piggy_service.get_goal(session, goal_id)
    return {
        "items": [
            {
                "id": row.id,
                "goal_id": row.goal_id,
                "amount_minor": row.amount_minor,
                "occurred_at": row.occurred_at.isoformat(),
                "tz_offset_minutes": row.tz_offset_minutes,
                "note": row.note,
            }
        ],
        "count": 1,
        "saved_minor": piggy_service.goal_saved(session, goal),
    }


@router.delete(
    "/goals/contributions/{contribution_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一笔注入",
)
def delete_contribution(contribution_id: int, session: Session = SessionDep) -> None:
    piggy_service.delete_contribution(session, contribution_id)
