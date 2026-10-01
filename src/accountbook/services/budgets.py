"""预算服务（P1 收尾）。

设计要点
--------
1. **"已用多少"永远现算，不落库**。预算的进度是流水的函数；
   存一份"已用金额"就要在每次记流水时同步它，一旦漏了，
   用户会看到一个不动的进度条 —— 比没有进度条更糟。
2. **结转（rollover）是上期算出来的**，只把结果缓存到
   ``carryover_minor``。它随期间变化，缓存只是为了避免每次打开
   界面都回溯全部历史。
3. **总预算与分类预算同时存在、各自独立判断**。把分类预算相加当作总预算
   会得出一个用户没设过的数（分类之间可以重叠，比如"餐饮"下面还有"外卖"）。
4. 数据为零时返回余额 = 额度、进度 = 0、天数照常算 ——
   "还没有支出"与"预算用完了"必须是两种完全不同的显示。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.domain import BudgetPeriod, BudgetScope
from ..core.errors import NotFoundError, ValidationError
from ..core.periods import BUDGET_PERIODS, period_bounds, shift_period
from ..db.models import Budget
from . import aggregate, audit

__all__ = [
    "budget_status",
    "create_budget",
    "delete_budget",
    "get_budget",
    "list_budgets",
    "overview",
    "previous_spend",
    "update_budget",
]

_logger = logging.getLogger(__name__)

_MUTABLE = frozenset(
    {
        "name",
        "scope",
        "category_id",
        "period",
        "amount_minor",
        "currency",
        "start_date",
        "end_date",
        "rollover",
        "alert_threshold",
        "enabled",
        "note",
    }
)


def list_budgets(session: Session, *, include_disabled: bool = True) -> list[Budget]:
    statement = select(Budget).where(Budget.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(Budget.enabled.is_(True))
    return list(session.scalars(statement.order_by(Budget.scope, Budget.id)).all())


def get_budget(session: Session, budget_id: int) -> Budget:
    budget = session.get(Budget, budget_id)
    if budget is None or budget.deleted_at is not None:
        raise NotFoundError("预算不存在", entity="budget", entity_id=budget_id)
    return budget


# -----------------------------------------------------------------------------
# 期间
# -----------------------------------------------------------------------------
def _resolve_bounds(budget: Budget, on: date) -> tuple[date, date]:
    """把预算解析成具体的起止日。

    ``custom`` 之外一律忽略用户填的起止日期 —— 否则会出现
    "月预算，但区间是去年某两个月"这种自相矛盾的状态。
    """
    if budget.period == BudgetPeriod.CUSTOM.value:
        start = budget.start_date or on
        end = budget.end_date or (start + timedelta(days=30))
        if end < start:
            raise ValidationError("自定义区间的结束日不能早于起始日", field="end_date", budget_id=budget.id)
        return start, end
    return period_bounds(budget.period, on)


def previous_spend(session: Session, budget: Budget, *, on: date) -> int:
    """上一期的实际支出（结转的计算依据）。"""
    if budget.period == BudgetPeriod.CUSTOM.value:
        # 自定义区间没有"上一期"的概念：自己定义的两段日期之间没有必然关系
        return 0
    previous_anchor = shift_period(budget.period, on, -1)
    start, end = period_bounds(budget.period, previous_anchor)
    return _spend(session, budget, start, end)


def _spend(session: Session, budget: Budget, start: date, end: date) -> int:
    category_ids = (
        {budget.category_id}
        if budget.scope == BudgetScope.CATEGORY.value and budget.category_id is not None
        else None
    )
    return aggregate.expense_total(session, start=start, end=end, category_ids=category_ids)


def budget_status(session: Session, budget: Budget, *, on: date | None = None) -> dict[str, Any]:
    """单个预算的完整状态。

    ``ratio`` 可以大于 1（超支）。刻意**不夹到 1** ——
    "用掉 130%" 与 "用掉 100%" 是完全不同的处境，
    把进度条夹在 100% 会把这个信息丢掉。
    """
    today = on or date.today()
    start, end = _resolve_bounds(budget, today)

    spent = _spend(session, budget, start, end)
    carryover = 0
    if budget.rollover:
        # 只结转**正**余额：上期超支不该把本期额度也吃掉，
        # 那会让一次意外支出连续惩罚两个期间
        available_before = budget.amount_minor + (budget.carryover_minor or 0)
        carryover = max(0, available_before - previous_spend(session, budget, on=today))

    total_available = budget.amount_minor + carryover
    remaining = total_available - spent
    ratio = (spent / total_available) if total_available > 0 else (1.0 if spent > 0 else 0.0)

    # 期间还剩几天（含今天）。用"含今天"是因为今天还能花，
    # 把它排除会让"每日可用"偏小，用户会不必要地收紧
    days_total = (end - start).days + 1
    days_left = max(0, (end - today).days + 1) if today <= end else 0
    daily_allowance = int(remaining / days_left) if days_left > 0 and remaining > 0 else 0

    return {
        "id": budget.id,
        "name": budget.name,
        "scope": budget.scope,
        "category_id": budget.category_id,
        "period": budget.period,
        "currency": budget.currency,
        "amount_minor": budget.amount_minor,
        "carryover_minor": carryover,
        "available_minor": total_available,
        "spent_minor": spent,
        "remaining_minor": remaining,
        "ratio": round(ratio, 4),
        "over": remaining < 0,
        # 数据为零时这两个字段本来就没意义，明确给 null 而不是编一个数字
        "alert": ratio >= budget.alert_threshold if total_available > 0 else False,
        "alert_threshold": budget.alert_threshold,
        "enabled": budget.enabled,
        "note": budget.note,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_total": days_total,
        "days_left": days_left,
        "daily_allowance_minor": daily_allowance,
        "is_current": start <= today <= end,
    }


def overview(session: Session, *, on: date | None = None) -> dict[str, Any]:
    """预算总览：逐条状态 + 总预算汇总 + 未设预算的提示。

    刻意**不**把分类预算相加成"总预算"：分类之间允许重叠
    （"餐饮"与"外卖"可能同时设了预算），相加会得出一个用户没设过的数。
    总预算只取 ``scope == total`` 的那一条（唯一约束保证只有一条启用中）。
    """
    today = on or date.today()
    budgets = list_budgets(session)
    items = [budget_status(session, item, on=today) for item in budgets]
    # 分类名由调用方（API 层）补，服务层不碰展示文案
    total = next((item for item in items if item["scope"] == BudgetScope.TOTAL.value), None)
    return {
        "date": today.isoformat(),
        "items": items,
        "total": total,
        "category_budgets": [item for item in items if item["scope"] == BudgetScope.CATEGORY.value],
        "alerts": [item for item in items if item["alert"] and item["is_current"] and item["enabled"]],
        "has_budget": bool(items),
    }


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    scope = payload.get("scope", BudgetScope.TOTAL.value)
    if scope not in {item.value for item in BudgetScope}:
        raise ValidationError(f"未知预算范围：{scope}", field="scope")
    if scope == BudgetScope.CATEGORY.value and not payload.get("category_id"):
        raise ValidationError("分类预算必须指定分类", field="category_id")

    period = payload.get("period", BudgetPeriod.MONTHLY.value)
    if period not in {*BUDGET_PERIODS, BudgetPeriod.CUSTOM.value}:
        raise ValidationError(f"未知预算期间：{period}", field="period")
    if period == BudgetPeriod.CUSTOM.value:
        if not payload.get("start_date") or not payload.get("end_date"):
            raise ValidationError("自定义期间必须给出起止日期", field="start_date")
        if payload["end_date"] < payload["start_date"]:
            raise ValidationError("结束日期不能早于起始日期", field="end_date")

    amount = payload.get("amount_minor")
    if amount is not None and amount < 0:
        raise ValidationError("预算额度不能为负数", field="amount_minor")
    threshold = payload.get("alert_threshold")
    if threshold is not None and not (0 < threshold <= 2):
        # 允许 >1：有人想"超支 20% 时再提醒我"
        raise ValidationError("提醒阈值应在 0 到 2 之间", field="alert_threshold")
    return payload


def create_budget(session: Session, **payload: Any) -> Budget:
    unknown = set(payload) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的预算字段：{sorted(unknown)}", fields=sorted(unknown))
    _validate(payload)

    budget = Budget(**payload)
    if not budget.name:
        budget.name = "预算"
    session.add(budget)
    session.flush()
    audit.record(
        session,
        entity="budget",
        entity_id=budget.id,
        action="create",
        changes={"name": {"to": budget.name}, "amount_minor": {"to": budget.amount_minor}},
    )
    return budget


def update_budget(session: Session, budget_id: int, **changes: Any) -> Budget:
    unknown = set(changes) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的预算字段：{sorted(unknown)}", fields=sorted(unknown))
    budget = get_budget(session, budget_id)

    merged = {field: getattr(budget, field) for field in _MUTABLE}
    merged.update(changes)
    _validate(merged)

    before = audit.snapshot(budget, changes.keys())
    for key, value in changes.items():
        setattr(budget, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity="budget",
        entity_id=budget_id,
        action="update",
        before=before,
        after={key: getattr(budget, key) for key in changes},
    )
    return budget


def delete_budget(session: Session, budget_id: int) -> None:
    budget = get_budget(session, budget_id)
    budget.soft_delete()
    session.flush()
    audit.record(session, entity="budget", entity_id=budget_id, action="delete")
