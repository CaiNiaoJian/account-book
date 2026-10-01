"""规划类路由：周期记账 / 预算 / 债务应收应付（P1 收尾）。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...services import budgets as budgets_service
from ...services import debts as debts_service
from ...services import recurring as recurring_service
from ..deps import get_session
from ..schemas import (
    BudgetCreate,
    BudgetOut,
    BudgetUpdate,
    DebtCreate,
    DebtOut,
    DebtPaymentCreate,
    DebtPaymentOut,
    DebtUpdate,
    RecurringRuleCreate,
    RecurringRuleOut,
    RecurringRuleUpdate,
    SettleRequest,
)

__all__ = ["router"]

router = APIRouter(tags=["planning"])

SessionDep = Depends(get_session)


# -----------------------------------------------------------------------------
# 周期记账
# -----------------------------------------------------------------------------
@router.get("/api/recurring", response_model=list[RecurringRuleOut], summary="周期规则列表")
def list_rules(
    include_disabled: bool = Query(default=True),
    session: Session = SessionDep,
) -> list[RecurringRuleOut]:
    return [
        RecurringRuleOut.model_validate(rule)
        for rule in recurring_service.list_rules(session, include_disabled=include_disabled)
    ]


@router.get("/api/recurring/upcoming", summary="即将到期 / 已逾期")
def upcoming(
    within_days: int = Query(default=7, ge=0, le=90),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """返回即将到期的规则。

    逾期项也在里面（``overdue=true``）—— 让"该记还没记"这件事留在视野里，
    而不是从列表里悄悄消失。
    """
    items = recurring_service.upcoming(session, within_days=within_days)
    return {
        "items": [
            {
                "rule_id": item["rule"].id,
                "name": item["rule"].name,
                "type": item["rule"].type,
                "amount_minor": item["rule"].amount_minor,
                "currency": item["rule"].currency,
                "due_date": item["due_date"].isoformat(),
                "days_until": item["days_until"],
                "overdue": item["overdue"],
                "auto_post": item["auto_post"],
            }
            for item in items
        ],
        "count": len(items),
    }


@router.post(
    "/api/recurring",
    response_model=RecurringRuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建周期规则",
)
def create_rule(payload: RecurringRuleCreate, session: Session = SessionDep) -> RecurringRuleOut:
    rule = recurring_service.create_rule(session, **payload.model_dump())
    return RecurringRuleOut.model_validate(rule)


@router.patch("/api/recurring/{rule_id}", response_model=RecurringRuleOut, summary="更新周期规则")
def update_rule(
    rule_id: int, payload: RecurringRuleUpdate, session: Session = SessionDep
) -> RecurringRuleOut:
    rule = recurring_service.update_rule(session, rule_id, **payload.model_dump(exclude_unset=True))
    return RecurringRuleOut.model_validate(rule)


@router.delete("/api/recurring/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除周期规则")
def delete_rule(rule_id: int, session: Session = SessionDep) -> None:
    recurring_service.delete_rule(session, rule_id)


@router.post("/api/recurring/post", summary="把到期的规则生成为流水")
def post_due(
    on: date | None = Query(default=None),
    rule_id: int | None = Query(default=None),
    dry_run: bool = Query(default=False, description="只报告将要生成什么，不写库"),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """生成到期流水。

    ``dry_run=true`` 让界面先告诉用户"将补记 3 笔"，确认后再真正写入 ——
    尤其是补记历史时，用户需要先看到后果。
    """
    return recurring_service.post_due(session, on=on, rule_id=rule_id, dry_run=dry_run)


# -----------------------------------------------------------------------------
# 预算
# -----------------------------------------------------------------------------
@router.get("/api/budgets", summary="预算总览（含每条进度）")
def budget_overview(
    on: date | None = Query(default=None),
    session: Session = SessionDep,
) -> dict[str, Any]:
    payload = budgets_service.overview(session, on=on)
    from ...services.categories import get_category

    names: dict[int, str] = {}
    for item in payload["items"]:
        category_id = item["category_id"]
        if category_id and category_id not in names:
            try:
                names[category_id] = get_category(session, category_id).name
            except Exception:  # noqa: BLE001 - 分类被删时不该让整页失败
                names[category_id] = ""
    for item in payload["items"]:
        item["category_name"] = names.get(item["category_id"] or 0, "")
    return payload


@router.post(
    "/api/budgets",
    response_model=BudgetOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建预算",
)
def create_budget(payload: BudgetCreate, session: Session = SessionDep) -> BudgetOut:
    budget = budgets_service.create_budget(session, **payload.model_dump())
    return BudgetOut.model_validate(budget)


@router.patch("/api/budgets/{budget_id}", response_model=BudgetOut, summary="更新预算")
def update_budget(budget_id: int, payload: BudgetUpdate, session: Session = SessionDep) -> BudgetOut:
    budget = budgets_service.update_budget(session, budget_id, **payload.model_dump(exclude_unset=True))
    return BudgetOut.model_validate(budget)


@router.delete("/api/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除预算")
def delete_budget(budget_id: int, session: Session = SessionDep) -> None:
    budgets_service.delete_budget(session, budget_id)


# -----------------------------------------------------------------------------
# 债务与应收应付
# -----------------------------------------------------------------------------
@router.get("/api/debts", summary="债务总览")
def debt_overview(
    on: date | None = Query(default=None),
    session: Session = SessionDep,
) -> dict[str, Any]:
    return debts_service.overview(session, on=on)


@router.get("/api/debts/{debt_id}/payments", response_model=list[DebtPaymentOut], summary="还款记录")
def list_payments(debt_id: int, session: Session = SessionDep) -> list[DebtPaymentOut]:
    debts_service.get_debt(session, debt_id)
    return [
        DebtPaymentOut.model_validate(payment) for payment in debts_service.list_payments(session, debt_id)
    ]


@router.post(
    "/api/debts",
    response_model=DebtOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建债务",
)
def create_debt(payload: DebtCreate, session: Session = SessionDep) -> DebtOut:
    data = payload.model_dump()
    create_mirror = bool(data.pop("create_mirror_account", False))
    debt = debts_service.create_debt(session, create_mirror_account=create_mirror, **data)
    return DebtOut.model_validate(debt)


@router.patch("/api/debts/{debt_id}", response_model=DebtOut, summary="更新债务")
def update_debt(debt_id: int, payload: DebtUpdate, session: Session = SessionDep) -> DebtOut:
    debt = debts_service.update_debt(session, debt_id, **payload.model_dump(exclude_unset=True))
    return DebtOut.model_validate(debt)


@router.delete("/api/debts/{debt_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除债务")
def delete_debt(debt_id: int, session: Session = SessionDep) -> None:
    debts_service.delete_debt(session, debt_id)


@router.post(
    "/api/debts/{debt_id}/payments",
    response_model=DebtPaymentOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记还款 / 收款",
)
def add_payment(debt_id: int, payload: DebtPaymentCreate, session: Session = SessionDep) -> DebtPaymentOut:
    payment = debts_service.add_payment(session, debt_id, **payload.model_dump())
    return DebtPaymentOut.model_validate(payment)


@router.delete(
    "/api/debts/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除还款记录",
)
def delete_payment(payment_id: int, session: Session = SessionDep) -> None:
    debts_service.delete_payment(session, payment_id)


@router.post("/api/debts/{debt_id}/settle", response_model=DebtOut, summary="结清 / 核销 / 重新激活")
def settle_debt(debt_id: int, payload: SettleRequest, session: Session = SessionDep) -> DebtOut:
    debt = debts_service.settle_debt(session, debt_id, status=payload.status, on=payload.on)
    return DebtOut.model_validate(debt)
