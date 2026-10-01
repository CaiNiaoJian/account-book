"""债务与应收应付服务（P1 收尾）。

为什么本金与利息必须分开
------------------------
把一笔还款记成"已还 3000"之后，就再也算不出"还剩多少本金"。
而"还剩多少本金"正是债务最核心的一个数 —— 用户打开这一页就是想知道它。
因此 ``debt_payments`` 把 ``principal_minor`` 与 ``interest_minor`` 分开存，
``amount_minor`` 只是二者的和（冗余但便于累加与对账）。

为什么创建债务时可选地生成一个应收/应付账户
--------------------------------------------
用户借出去 5000 元，如果只写一条债务记录，净值不会变 ——
他会觉得"我明明少了 5000，怎么净资产没动"。
因此可选地建一个 ``receivable``（借出）或 ``payable``（借入）账户承载它，
让这笔钱真实地进入资产负债表。这一步是**显式可选**的，
因为有人只想把债务当备忘录用，不想影响净值。

数据为零时
----------
没有债务 → 空列表 + 汇总为 0；有债务但还没还过 → 剩余本金等于原始本金，
且明确区分"未到期 / 已逾期"。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.domain import AccountType, DebtKind, DebtStatus
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import Account, Debt, DebtPayment
from . import accounts as accounts_service
from . import audit

__all__ = [
    "add_payment",
    "create_debt",
    "delete_debt",
    "delete_payment",
    "get_debt",
    "list_debts",
    "overview",
    "settle_debt",
    "update_debt",
]

_logger = logging.getLogger(__name__)

_MUTABLE = frozenset(
    {
        "name",
        "kind",
        "counterparty",
        "principal_minor",
        "currency",
        "account_id",
        "mirror_account_id",
        "start_date",
        "due_date",
        "annual_rate_bps",
        "note",
    }
)

_KINDS = {item.value for item in DebtKind}
_STATUSES = {item.value for item in DebtStatus}


# -----------------------------------------------------------------------------
# 读取
# -----------------------------------------------------------------------------
def list_debts(session: Session, *, status: str | None = None) -> list[Debt]:
    statement = select(Debt).where(Debt.deleted_at.is_(None))
    if status is not None:
        statement = statement.where(Debt.status == status)
    return list(session.scalars(statement.order_by(Debt.due_date, Debt.id)).all())


def get_debt(session: Session, debt_id: int) -> Debt:
    debt = session.get(Debt, debt_id)
    if debt is None or debt.deleted_at is not None:
        raise NotFoundError("债务记录不存在", entity="debt", entity_id=debt_id)
    return debt


def _totals(session: Session, debt_id: int) -> dict[str, int]:
    """已还本金 / 利息 / 总额。**一次查询算完**，不逐条加载。"""
    row = session.execute(
        select(
            func.coalesce(func.sum(DebtPayment.principal_minor), 0),
            func.coalesce(func.sum(DebtPayment.interest_minor), 0),
            func.coalesce(func.sum(DebtPayment.amount_minor), 0),
            func.count(DebtPayment.id),
        ).where(DebtPayment.debt_id == debt_id)
    ).one()
    return {
        "paid_principal_minor": int(row[0]),
        "paid_interest_minor": int(row[1]),
        "paid_total_minor": int(row[2]),
        "payment_count": int(row[3]),
    }


def debt_status(session: Session, debt: Debt, *, on: date | None = None) -> dict[str, Any]:
    """单条债务的完整状态。

    ``remaining_minor`` 可能为负（多还了），**不夹到 0**：
    多还通常意味着记错了或者对方退了钱，把它藏起来会让用户对不上账。
    """
    today = on or date.today()
    totals = _totals(session, debt.id)
    remaining = debt.principal_minor - totals["paid_principal_minor"]
    progress = (
        min(1.0, totals["paid_principal_minor"] / debt.principal_minor) if debt.principal_minor > 0 else 1.0
    )
    overdue = (
        debt.status == DebtStatus.ACTIVE.value
        and debt.due_date is not None
        and debt.due_date < today
        and remaining > 0
    )
    days_until = (debt.due_date - today).days if debt.due_date is not None else None
    # 按年化利率估算的每日利息（基点 → 万分比）。
    # 只是估算：真实计息方式（等额本息/先息后本）不在本应用范围内，
    # 界面上必须写明"估算"二字
    daily_interest = round(remaining * (debt.annual_rate_bps or 0) / 10_000 / 365)

    return {
        "id": debt.id,
        "name": debt.name,
        "kind": debt.kind,
        "counterparty": debt.counterparty,
        "principal_minor": debt.principal_minor,
        "currency": debt.currency,
        "account_id": debt.account_id,
        "mirror_account_id": debt.mirror_account_id,
        "start_date": debt.start_date.isoformat(),
        "due_date": debt.due_date.isoformat() if debt.due_date else None,
        "annual_rate_bps": debt.annual_rate_bps,
        "status": debt.status,
        "settled_at": debt.settled_at.isoformat() if debt.settled_at else None,
        "note": debt.note,
        **totals,
        "remaining_minor": remaining,
        "progress": round(progress, 4),
        "overdue": overdue,
        "days_until_due": days_until,
        "estimated_daily_interest_minor": daily_interest,
    }


def overview(session: Session, *, on: date | None = None) -> dict[str, Any]:
    """债务总览：分应收/应付汇总 + 到期提醒 + 逐条状态。"""
    today = on or date.today()
    items = [debt_status(session, debt, on=today) for debt in list_debts(session)]

    receivable = [item for item in items if item["kind"] == DebtKind.LEND.value]
    payable = [item for item in items if item["kind"] == DebtKind.BORROW.value]

    def _outstanding(group: list[dict[str, Any]]) -> int:
        return sum(item["remaining_minor"] for item in group if item["status"] == "active")

    upcoming = sorted(
        (item for item in items if item["status"] == "active" and item["due_date"] is not None),
        key=lambda item: item["due_date"] or "",
    )
    return {
        "date": today.isoformat(),
        "items": items,
        "receivable": receivable,
        "payable": payable,
        "summary": {
            "receivable_minor": _outstanding(receivable),
            "payable_minor": _outstanding(payable),
            "net_minor": _outstanding(receivable) - _outstanding(payable),
            "active_count": sum(1 for item in items if item["status"] == "active"),
            "overdue_count": sum(1 for item in items if item["overdue"]),
            "settled_count": sum(1 for item in items if item["status"] == "settled"),
        },
        "overdue": [item for item in items if item["overdue"]],
        "upcoming": [item for item in upcoming if not item["overdue"]][:10],
        "has_debt": bool(items),
    }


def list_payments(session: Session, debt_id: int) -> list[DebtPayment]:
    return list(
        session.scalars(
            select(DebtPayment)
            .where(DebtPayment.debt_id == debt_id)
            .order_by(DebtPayment.occurred_at, DebtPayment.id)
        ).all()
    )


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def _validate(payload: dict[str, Any], session: Session) -> None:
    kind = payload.get("kind")
    if kind is not None and kind not in _KINDS:
        raise ValidationError(f"未知的债务方向：{kind}", field="kind")
    for field in ("principal_minor", "annual_rate_bps"):
        value = payload.get(field)
        if value is not None and value < 0:
            raise ValidationError(f"{field} 不能为负数", field=field)
    due = payload.get("due_date")
    start = payload.get("start_date")
    if due and start and due < start:
        raise ValidationError("到期日不能早于起始日", field="due_date")
    for field in ("account_id", "mirror_account_id"):
        account_id = payload.get(field)
        if account_id is None:
            continue
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)


def create_debt(session: Session, *, create_mirror_account: bool = False, **payload: Any) -> Debt:
    """新建债务。

    ``create_mirror_account=True`` 时同时建一个应收/应付账户，
    让这笔钱进入净值（见模块说明）。
    """
    unknown = set(payload) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的债务字段：{sorted(unknown)}", fields=sorted(unknown))
    _validate(payload, session)

    debt = Debt(**payload)
    if create_mirror_account and debt.mirror_account_id is None:
        mirror_type = (
            AccountType.RECEIVABLE.value if debt.kind == DebtKind.LEND.value else AccountType.PAYABLE.value
        )
        # 借出是资产（余额为正），借入是负债（余额为负）——
        # 与账户服务"按余额符号分类资产负债"的规则一致
        balance = debt.principal_minor if debt.kind == DebtKind.LEND.value else -debt.principal_minor
        mirror = accounts_service.create_account(
            session,
            name=f"{debt.counterparty or debt.name}",
            type=mirror_type,
            initial_balance_minor=balance,
            currency=debt.currency,
            note=f"由债务「{debt.name}」自动创建",
        )
        debt.mirror_account_id = mirror.id

    session.add(debt)
    session.flush()
    audit.record(
        session,
        entity="debt",
        entity_id=debt.id,
        action="create",
        changes={"name": {"to": debt.name}, "kind": {"to": debt.kind}},
    )
    return debt


def update_debt(session: Session, debt_id: int, **changes: Any) -> Debt:
    unknown = set(changes) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的债务字段：{sorted(unknown)}", fields=sorted(unknown))
    debt = get_debt(session, debt_id)

    merged = {field: getattr(debt, field) for field in _MUTABLE}
    merged.update(changes)
    _validate(merged, session)

    before = audit.snapshot(debt, changes.keys())
    for key, value in changes.items():
        setattr(debt, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity="debt",
        entity_id=debt_id,
        action="update",
        before=before,
        after={key: getattr(debt, key) for key in changes},
    )
    return debt


def delete_debt(session: Session, debt_id: int) -> None:
    debt = get_debt(session, debt_id)
    debt.soft_delete()
    session.flush()
    audit.record(session, entity="debt", entity_id=debt_id, action="delete")


def add_payment(
    session: Session,
    debt_id: int,
    *,
    amount_minor: int,
    occurred_at: datetime,
    principal_minor: int | None = None,
    interest_minor: int | None = None,
    account_id: int | None = None,
    note: str = "",
    tz_offset_minutes: int = 0,
) -> DebtPayment:
    """登记一次还款 / 收款。

    ``principal_minor`` 与 ``interest_minor`` 都不给时，**整笔视为本金**。
    这是最常见的记账方式，也是最安全的默认：把未指明的一笔记成利息会
    凭空产生支出，而记成本金只是保守。
    """
    debt = get_debt(session, debt_id)
    if debt.status == DebtStatus.SETTLED.value:
        raise ConflictError("已结清的债务不能再登记还款", debt_id=debt_id, suggestion="reopen")
    if amount_minor <= 0:
        raise ValidationError("还款金额必须大于 0", field="amount_minor")

    if principal_minor is None and interest_minor is None:
        principal_minor, interest_minor = amount_minor, 0
    elif principal_minor is None:
        principal_minor = amount_minor - (interest_minor or 0)
    elif interest_minor is None:
        interest_minor = amount_minor - principal_minor

    if principal_minor < 0 or interest_minor < 0:
        raise ValidationError("本金与利息都不能为负数", field="principal_minor")
    if principal_minor + interest_minor != amount_minor:
        raise ValidationError(
            "本金与利息之和必须等于还款金额",
            field="amount_minor",
            detail={"principal_minor": principal_minor, "interest_minor": interest_minor},
        )
    if account_id is not None:
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)

    payment = DebtPayment(
        debt_id=debt_id,
        amount_minor=amount_minor,
        principal_minor=principal_minor,
        interest_minor=interest_minor,
        occurred_at=occurred_at,
        tz_offset_minutes=tz_offset_minutes,
        account_id=account_id,
        note=note,
    )
    session.add(payment)
    session.flush()

    # 本金还完就自动结清 —— 但仍保留手动结清的入口（有人会提前核销）
    remaining = debt.principal_minor - _totals(session, debt_id)["paid_principal_minor"]
    if remaining <= 0:
        debt.status = DebtStatus.SETTLED.value
        debt.settled_at = occurred_at.date()

    audit.record(
        session,
        entity="debt",
        entity_id=debt_id,
        action="payment",
        changes={"amount_minor": {"to": amount_minor}, "principal_minor": {"to": principal_minor}},
    )
    session.flush()
    return payment


def delete_payment(session: Session, payment_id: int) -> None:
    """删除一次还款。**物理删除** —— 它不是账目事实，只是债务的进度标记。"""
    payment = session.get(DebtPayment, payment_id)
    if payment is None:
        raise NotFoundError("还款记录不存在", entity="debt_payment", entity_id=payment_id)
    debt_id = payment.debt_id
    session.delete(payment)
    session.flush()
    # 删掉还款后债务可能重新变成未结清，状态必须跟着回退
    debt = session.get(Debt, debt_id)
    if debt is not None and debt.status == DebtStatus.SETTLED.value:
        remaining = debt.principal_minor - _totals(session, debt_id)["paid_principal_minor"]
        if remaining > 0:
            debt.status = DebtStatus.ACTIVE.value
            debt.settled_at = None
    session.flush()
    audit.record(session, entity="debt", entity_id=debt_id, action="payment_delete")


def settle_debt(session: Session, debt_id: int, *, status: str = "settled", on: date | None = None) -> Debt:
    """手动结清 / 核销 / 重新激活。"""
    if status not in _STATUSES:
        raise ValidationError(f"未知状态：{status}", field="status")
    debt = get_debt(session, debt_id)
    debt.status = status
    debt.settled_at = (on or date.today()) if status != DebtStatus.ACTIVE.value else None
    session.flush()
    audit.record(
        session,
        entity="debt",
        entity_id=debt_id,
        action="settle",
        changes={"status": {"to": status}},
    )
    return debt
