"""薪酬（P6 / 需求 18、19）。

一条数据流
==========
`pay_sources`（来源）→ `pay_components`（组成模板）→ 计算 → `payroll_records`（快照）
→ 填写完成时生成一笔入账流水。每一步都能回看，因此"这笔工资是怎么算出来的"
永远可复现。

三个关键决定
============

**一、组成项要分两趟算。**
先算所有加项得到应发合计，再算减项。因为真实的工资表就是这样的：
个税与五险一金代扣都是**基于应发合计**算的。
一趟算完的话，一个引用"应发"的减项会拿到一个"只加到一半"的值 ——
而这种错误不会报错，只会算出一个偏小的数字。

**二、公式用 JSON 表达式树，不用 `eval()`。**
工资表是可以被**导入**的数据。`eval()` 会把一份可导入的数据变成
任意代码执行入口。表达式树能做到同样的事，而且"支持哪些运算"
是一份可见的白名单。

**三、收录记录存的是快照，不是指向模板的外键。**
模板会变（涨薪、改比例）。"去年 3 月那笔是怎么算出来的"必须能原样复现，
而指向模板等于让历史随模板一起变。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.domain import TransactionSource, TransactionType
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import (
    Account,
    Category,
    PayComponent,
    PaydayRule,
    PayrollRecord,
    PaySource,
)
from . import transactions as transactions_service
from . import workdays

__all__ = [
    "COMPONENT_CALCS",
    "COMPONENT_KINDS",
    "SOURCE_KINDS",
    "add_source",
    "compute_payroll",
    "create_component",
    "create_record",
    "delete_component",
    "delete_record",
    "delete_source",
    "ensure_record_for_period",
    "evaluate_formula",
    "fill_record",
    "get_record",
    "get_source",
    "history",
    "list_components",
    "list_records",
    "list_sources",
    "month_bounds",
    "pay_date_for",
    "payroll_overview",
    "pending_records",
    "recompute_record",
    "restore_source",
    "serialize_component",
    "serialize_rule",
    "serialize_source",
    "skip_record",
    "upcoming",
    "update_component",
    "update_record",
    "update_source",
    "upsert_payday_rule",
    "validate_formula_shape",
]

_logger = logging.getLogger(__name__)

SOURCE_KINDS = ("salary", "part_time", "bonus", "investment", "rent", "other")
COMPONENT_KINDS = (
    "basic",
    "performance",
    "overtime",
    "meal",
    "transport",
    "bonus",
    "commission",
    "reimbursement",
    "pretax_deduction",
    "tax",
    "insurance",
    "other",
)
COMPONENT_CALCS = ("fixed", "ratio", "formula")

#: 公式表达式树的最大深度。防的是坏数据（导入的畸形 JSON）把求值拖进深递归
_MAX_FORMULA_DEPTH = 8

#: 可用的运算。**白名单**：不在表里的运算直接报错，而不是被忽略
_FORMULA_OPS = {
    "add": "加法",
    "sub": "减法",
    "mul": "乘法",
    "div": "除法",
    "min": "取小",
    "max": "取大",
}

#: 金额运算统一用 Decimal，最后一次性取整到最小单位。
#: 逐步取整会让"按比例算的三个加项"累计出几分钱的偏差，
#: 而工资表上差几分钱是最容易引起争议的。
_MONEY_QUANT = Decimal("1")


def _q(value: Decimal) -> int:
    return int(value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP))


# -----------------------------------------------------------------------------
# 公式求值
# -----------------------------------------------------------------------------
def _formula_value(node: Any, context: dict[str, int], depth: int = 0) -> Decimal:
    """求值一个 JSON 表达式节点。

    支持的字面量与引用：
    * ``{"const": 1234}``  —— 最小单位金额；
    * ``{"ratio": 1000}``  —— 万分比（1000 = 10%）；
    * ``{"var": "basic"}`` —— basic / gross / net_so_far；
    * ``{"ref": "绩效"}``  —— 另一个组成项的已算结果。
    """
    if depth > _MAX_FORMULA_DEPTH:
        raise ValidationError("公式嵌套过深", field="formula", max_depth=_MAX_FORMULA_DEPTH)
    if not isinstance(node, dict):
        raise ValidationError(f"公式节点必须是对象，收到 {type(node).__name__}", field="formula")

    if "const" in node:
        return Decimal(int(node["const"]))
    if "ratio" in node:
        return Decimal(int(node["ratio"])) / Decimal(10000)
    if "var" in node:
        key = str(node["var"])
        if key not in context:
            raise ValidationError(f"公式引用了未知变量：{key}", field="formula", available=sorted(context))
        return Decimal(context[key])
    if "ref" in node:
        key = str(node["ref"])
        if key not in context:
            raise ValidationError(
                f"公式引用了尚未计算的组成项：{key}", field="formula", available=sorted(context)
            )
        return Decimal(context[key])
    if "op" in node:
        op = str(node["op"])
        if op not in _FORMULA_OPS:
            raise ValidationError(f"未知的运算：{op}", field="formula", allowed=sorted(_FORMULA_OPS))
        raw_args = node.get("args")
        if not isinstance(raw_args, list) or not raw_args:
            raise ValidationError(f"运算 {op} 需要至少一个参数", field="formula")
        values = [_formula_value(item, context, depth + 1) for item in raw_args]
        try:
            if op == "add":
                total = Decimal(0)
                for value in values:
                    total += value
                return total
            if op == "sub":
                total = values[0]
                for value in values[1:]:
                    total -= value
                return total
            if op == "mul":
                total = Decimal(1)
                for value in values:
                    total *= value
                return total
            if op == "div":
                total = values[0]
                for value in values[1:]:
                    if value == 0:
                        # 除零在工资表里几乎总是配置错误（比如把比例填成 0），
                        # 静默返回 0 会让"这一项没扣"看起来像正常结果
                        raise ValidationError("公式中出现除以零", field="formula", op=op)
                    total /= value
                return total
            if op == "min":
                return min(values)
            return max(values)
        except (DivisionByZero, InvalidOperation) as error:  # pragma: no cover - 已被上面拦住
            raise ValidationError(f"公式计算出错：{error}", field="formula") from error
    raise ValidationError(
        "公式节点必须包含 const / ratio / var / ref / op 之一", field="formula", node=sorted(node)
    )


def validate_formula_shape(node: Any, depth: int = 0) -> None:
    """只校验**结构**：运算在白名单里、节点形状合法、嵌套不深。

    `create_component` 用它做保存前的检查，而**不**去解析变量引用 ——
    因为创建时还不知道有哪些兄弟组成项，
    在那里解析会让"绩效 = 基本工资 × 20%"这种再正常不过的公式存不下来。
    引用是否存在只能在计算时判断（那时 context 才是完整的）。
    """
    if depth > _MAX_FORMULA_DEPTH:
        raise ValidationError("公式嵌套过深", field="formula", max_depth=_MAX_FORMULA_DEPTH)
    if not isinstance(node, dict):
        raise ValidationError(f"公式节点必须是对象，收到 {type(node).__name__}", field="formula")
    if "op" in node:
        op = str(node["op"])
        if op not in _FORMULA_OPS:
            raise ValidationError(f"未知的运算：{op}", field="formula", allowed=sorted(_FORMULA_OPS))
        raw_args = node.get("args")
        if not isinstance(raw_args, list) or not raw_args:
            raise ValidationError(f"运算 {op} 需要至少一个参数", field="formula")
        for item in raw_args:
            validate_formula_shape(item, depth + 1)
        return
    keys = {"const", "ratio", "var", "ref"}
    if not (keys & set(node)):
        raise ValidationError(
            "公式节点必须包含 const / ratio / var / ref / op 之一", field="formula", node=sorted(node)
        )
    # 只用比例、没有任何金额来源的公式几乎总是配错了：
    # 它的值是 0.1 这种小数，取整后静默变成 0。
    # **只在顶层判断**：作为参数时 `{"ratio": 1000}` 是完全正常的乘数，
    # 例如 `mul(basic, 10%)` —— 在参数位置也拦会把正确的公式一起拒掉。
    if depth == 0 and "ratio" in node and not any(key in node for key in ("const", "var", "ref")):
        raise ValidationError(
            "公式里只有比例、没有金额来源（如 const / var / ref），结果会一直是 0",
            field="formula",
            hint='比例要乘以某个金额，例如 {"op": "mul", "args": [{"var": "basic"}, {"ratio": 1000}]}',
        )


def evaluate_formula(formula: Any, context: dict[str, int]) -> int:
    """求值并取整到最小单位。"""
    if not formula:
        return 0
    return _q(_formula_value(formula, context))


# -----------------------------------------------------------------------------
# 来源
# -----------------------------------------------------------------------------
def get_source(session: Session, source_id: int) -> PaySource:
    row = session.get(PaySource, source_id)
    if row is None or row.deleted_at is not None:
        raise NotFoundError("薪资来源不存在", entity="pay_source", entity_id=source_id)
    return row


def add_source(
    session: Session,
    *,
    name: str,
    kind: str = "salary",
    employer: str = "",
    account_id: int | None = None,
    category_id: int | None = None,
    priority: int = 5,
    note: str = "",
) -> PaySource:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("来源名称不能为空", field="name")
    if kind not in SOURCE_KINDS:
        raise ValidationError(f"未知的来源类型：{kind}", field="kind", allowed=list(SOURCE_KINDS))
    if not 0 <= priority <= 9:
        raise ValidationError("优先级必须在 0–9 之间", field="priority")
    if account_id is not None:
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)
    if category_id is not None and session.get(Category, category_id) is None:
        raise NotFoundError("分类不存在", entity="category", entity_id=category_id)

    row = PaySource(
        name=cleaned,
        kind=kind,
        employer=(employer or "").strip(),
        account_id=account_id,
        category_id=category_id,
        priority=priority,
        note=(note or "").strip(),
    )
    session.add(row)
    session.flush()
    return row


def update_source(session: Session, source_id: int, **changes: Any) -> PaySource:
    row = get_source(session, source_id)
    mutable = {
        "name",
        "kind",
        "employer",
        "account_id",
        "category_id",
        "enabled",
        "priority",
        "sort_order",
        "note",
    }
    for key, value in changes.items():
        if key not in mutable:
            continue
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise ValidationError("来源名称不能为空", field="name")
        if key == "kind" and value not in SOURCE_KINDS:
            raise ValidationError(f"未知的来源类型：{value}", field="kind")
        if key == "account_id" and value is not None:
            account = session.get(Account, int(value))
            if account is None or account.deleted_at is not None:
                raise NotFoundError("账户不存在", entity="account", entity_id=value)
        setattr(row, key, value)
    session.flush()
    return row


def list_sources(session: Session, *, include_disabled: bool = True) -> list[PaySource]:
    statement = select(PaySource).where(PaySource.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(PaySource.enabled.is_(True))
    return list(
        session.scalars(
            statement.order_by(PaySource.priority.desc(), PaySource.sort_order, PaySource.id)
        ).all()
    )


def delete_source(session: Session, source_id: int) -> None:
    row = get_source(session, source_id)
    row.deleted_at = datetime.now()
    session.flush()


def restore_source(session: Session, source_id: int) -> PaySource:
    row = session.get(PaySource, source_id)
    if row is None:
        raise NotFoundError("薪资来源不存在", entity="pay_source", entity_id=source_id)
    row.deleted_at = None
    session.flush()
    return row


# -----------------------------------------------------------------------------
# 组成项
# -----------------------------------------------------------------------------
def create_component(
    session: Session,
    *,
    name: str,
    kind: str = "other",
    calc: str = "fixed",
    sign: int = 1,
    amount_minor: int = 0,
    base_key: str = "basic",
    rate_bps: int = 0,
    formula: dict[str, Any] | None = None,
    source_id: int | None = None,
    sort_order: int = 0,
    enabled: bool = True,
) -> PayComponent:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("组成项名称不能为空", field="name")
    if kind not in COMPONENT_KINDS:
        raise ValidationError(f"未知的组成项类型：{kind}", field="kind", allowed=list(COMPONENT_KINDS))
    if calc not in COMPONENT_CALCS:
        raise ValidationError(f"未知的计算方式：{calc}", field="calc", allowed=list(COMPONENT_CALCS))
    if sign not in (1, -1):
        raise ValidationError("sign 只能是 1（加项）或 -1（减项）", field="sign")
    if amount_minor < 0:
        raise ValidationError("金额不能为负（方向由 sign 决定）", field="amount_minor")
    if not 0 <= rate_bps <= 100000:
        raise ValidationError("比例必须在 0–1000% 之间", field="rate_bps")
    if base_key not in {"basic", "gross"}:
        raise ValidationError("比例基数只能是 basic 或 gross", field="base_key")
    if calc == "formula":
        # 只校验结构：运算白名单、节点形状、嵌套深度。
        # **不解析引用** —— 创建时还看不到兄弟组成项（见 validate_formula_shape）
        validate_formula_shape(formula or {})
    if source_id is not None:
        get_source(session, source_id)

    row = PayComponent(
        source_id=source_id,
        name=cleaned,
        kind=kind,
        calc=calc,
        sign=sign,
        amount_minor=amount_minor,
        base_key=base_key,
        rate_bps=rate_bps,
        formula=formula or {},
        enabled=enabled,
        sort_order=sort_order,
    )
    session.add(row)
    session.flush()
    return row


def update_component(session: Session, component_id: int, **changes: Any) -> PayComponent:
    row = session.get(PayComponent, component_id)
    if row is None:
        raise NotFoundError("组成项不存在", entity="pay_component", entity_id=component_id)
    for key, value in changes.items():
        if key not in {
            "name",
            "kind",
            "calc",
            "sign",
            "amount_minor",
            "base_key",
            "rate_bps",
            "formula",
            "enabled",
            "sort_order",
        }:
            continue
        setattr(row, key, value)
    session.flush()
    return row


def list_components(
    session: Session, *, source_id: int | None = None, include_disabled: bool = True
) -> list[PayComponent]:
    statement = select(PayComponent)
    if source_id is not None:
        # 来源专属项 + 全局共享项（source_id 为空）
        statement = statement.where(
            (PayComponent.source_id == source_id) | (PayComponent.source_id.is_(None))
        )
    if not include_disabled:
        statement = statement.where(PayComponent.enabled.is_(True))
    return list(session.scalars(statement.order_by(PayComponent.sort_order, PayComponent.id)).all())


def delete_component(session: Session, component_id: int) -> None:
    row = session.get(PayComponent, component_id)
    if row is None:
        raise NotFoundError("组成项不存在", entity="pay_component", entity_id=component_id)
    session.delete(row)
    session.flush()


# -----------------------------------------------------------------------------
# 金额计算
# -----------------------------------------------------------------------------
def _component_amount(component: PayComponent, context: dict[str, int]) -> int:
    if component.calc == "fixed":
        return int(component.amount_minor)
    if component.calc == "ratio":
        base = context.get(component.base_key, 0)
        return _q(Decimal(int(base)) * Decimal(int(component.rate_bps)) / Decimal(10000))
    return evaluate_formula(component.formula, context)


def compute_payroll(
    session: Session,
    source_id: int,
    *,
    overrides: dict[int, int] | None = None,
) -> dict[str, Any]:
    """按组成模板算出应发、实发与逐项明细。

    **两趟算**：先所有加项（得到应发合计），再所有减项。
    真实的工资表就是这样 —— 个税与五险一金代扣都基于应发合计。
    一趟算完的话，一个引用"应发"的减项会拿到一个只加到一半的值，
    而这种错误不会报错，只会算出一个偏小的数字。
    """
    get_source(session, source_id)
    components = list_components(session, source_id=source_id, include_disabled=False)
    overrides = overrides or {}

    add_ons = [item for item in components if item.sign == 1]
    deductions = [item for item in components if item.sign == -1]

    context: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    gross = 0
    for component in add_ons:
        amount = (
            int(overrides[component.id])
            if component.id in overrides
            else _component_amount(component, context)
        )
        gross += amount
        # 后面的项可以按名字引用前面的项（"绩效 = 基本工资 × 20%"）
        context[component.name] = amount
        context["basic"] = sum(context.get(item.name, 0) for item in add_ons if item.kind == "basic")
        context["gross"] = gross
        context["net_so_far"] = gross
        items.append(_item_payload(component, amount, "add"))

    basic_total = sum(context.get(item.name, 0) for item in add_ons if item.kind == "basic")
    # 减项阶段看到的 basic/gross 必须是**最终**值
    context["basic"] = basic_total
    context["gross"] = gross

    deducted = 0
    for component in deductions:
        amount = (
            int(overrides[component.id])
            if component.id in overrides
            else _component_amount(component, context)
        )
        # 减项不该把实发扣成负数：那说明配置有问题，而不是"这个月白干"
        if deducted + amount > gross:
            raise ConflictError(
                f"减项合计超过应发合计（「{component.name}」这一步）",
                entity="payroll",
                source_id=source_id,
                gross_minor=gross,
                deducted_minor=deducted,
                attempted_minor=amount,
                suggestion="检查个税与五险一金代扣的配置是否重复计算",
            )
        deducted += amount
        context[component.name] = amount
        context["net_so_far"] = gross - deducted
        items.append(_item_payload(component, amount, "deduct"))

    return {
        "source_id": source_id,
        "gross_minor": gross,
        "deduction_minor": deducted,
        "net_minor": gross - deducted,
        "items": items,
        "basic_minor": basic_total,
        "tax_minor": sum(item["amount_minor"] for item in items if item["kind"] == "tax"),
        "insurance_minor": sum(item["amount_minor"] for item in items if item["kind"] == "insurance"),
    }


def _item_payload(component: PayComponent, amount: int, direction: str) -> dict[str, Any]:
    """明细项快照。把计算方式也存进去，将来才说得清"这笔是怎么来的"。"""
    return {
        "component_id": component.id,
        "name": component.name,
        "kind": component.kind,
        "sign": component.sign,
        "direction": direction,
        "calc": component.calc,
        "amount_minor": amount,
        "rate_bps": component.rate_bps,
        "base_key": component.base_key,
        "source_amount_minor": component.amount_minor,
    }


# -----------------------------------------------------------------------------
# 收录记录
# -----------------------------------------------------------------------------
def _ensure_period(period: str) -> str:
    text = (period or "").strip()
    if len(text) != 7 or text[4] != "-":
        raise ValidationError("期间格式应为 YYYY-MM", field="period", got=text)
    try:
        year, month = int(text[:4]), int(text[5:])
    except ValueError as error:
        raise ValidationError("期间格式应为 YYYY-MM", field="period", got=text) from error
    if not 1 <= month <= 12:
        raise ValidationError("月份必须在 1–12 之间", field="period", got=text)
    if year < 1900 or year > 2200:
        raise ValidationError("年份超出合理范围", field="period", got=text)
    return f"{year:04d}-{month:02d}"


def _period_month(period: str) -> tuple[int, int]:
    return int(period[:4]), int(period[5:])


def pay_date_for(session: Session, source_id: int, period: str) -> dict[str, Any]:
    """某来源某期间的发薪日。没有规则时回落到当月 15 日并**标明是推断的**。"""
    period = _ensure_period(period)
    year, month = _period_month(period)
    rule = session.scalar(
        select(PaydayRule).where(PaydayRule.source_id == source_id, PaydayRule.enabled.is_(True))
    )
    if rule is None:
        # 不给默认值会让"还没配规则"变成一个无法继续的状态；
        # 但默认出来的日期必须标明是推断的，不能装作算过
        return {
            "period": period,
            "base_date": date(year, month, 15).isoformat(),
            "pay_date": date(year, month, 15).isoformat(),
            "adjusted": False,
            "reason": "",
            "policy": "none",
            "shift_days": 0,
            "holiday_name": "",
            "confidence": "inferred",
        }
    payload = workdays.resolve_payday(session, rule, year, month).as_dict()
    payload["confidence"] = "exact" if payload.pop("confident") else "assumed"
    return payload


def create_record(
    session: Session,
    source_id: int,
    period: str,
    *,
    pay_date: date | None = None,
    overrides: dict[int, int] | None = None,
    status: str = "draft",
    note: str = "",
) -> PayrollRecord:
    """建一条收录记录（草稿）。**不生成流水** —— 那是 `fill_record` 的事。"""
    period = _ensure_period(period)
    get_source(session, source_id)
    existing = session.scalar(
        select(PayrollRecord).where(
            PayrollRecord.source_id == source_id,
            PayrollRecord.period == period,
            PayrollRecord.deleted_at.is_(None),
        )
    )
    if existing is not None:
        raise ConflictError(
            "这个来源在这一期已经有一条记录了",
            entity="payroll_record",
            record_id=existing.id,
            period=period,
            suggestion="直接编辑那一条，或先删除它",
        )

    computed = compute_payroll(session, source_id, overrides=overrides)
    resolved = pay_date or date.fromisoformat(pay_date_for(session, source_id, period)["pay_date"])
    row = PayrollRecord(
        source_id=source_id,
        period=period,
        pay_date=resolved,
        gross_minor=computed["gross_minor"],
        net_minor=computed["net_minor"],
        items=computed["items"],
        tax_minor=computed["tax_minor"],
        insurance_snapshot={"total_minor": computed["insurance_minor"]},
        status=status,
        note=(note or "").strip(),
    )
    session.add(row)
    session.flush()
    return row


def get_record(session: Session, record_id: int) -> PayrollRecord:
    row = session.get(PayrollRecord, record_id)
    if row is None or row.deleted_at is not None:
        raise NotFoundError("工资记录不存在", entity="payroll_record", entity_id=record_id)
    return row


def update_record(session: Session, record_id: int, **changes: Any) -> PayrollRecord:
    row = get_record(session, record_id)
    for key in ("pay_date", "gross_minor", "net_minor", "items", "tax_minor", "note"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    session.flush()
    return row


def recompute_record(session: Session, record_id: int) -> PayrollRecord:
    """按当前模板重算。

    只对**还没填写**的记录开放：已经入账的记录重算会让账目与记录脱节
    （流水金额不变而记录金额变了），那比"数字旧了"更糟。
    """
    row = get_record(session, record_id)
    if row.status == "filled":
        raise ConflictError(
            "已填写并入账的记录不能重算",
            entity="payroll_record",
            record_id=record_id,
            suggestion="先撤销入账（删除关联流水），或手工调整金额",
        )
    computed = compute_payroll(session, row.source_id)
    row.gross_minor = computed["gross_minor"]
    row.net_minor = computed["net_minor"]
    row.items = computed["items"]
    row.tax_minor = computed["tax_minor"]
    row.insurance_snapshot = {"total_minor": computed["insurance_minor"]}
    session.flush()
    return row


def fill_record(
    session: Session,
    record_id: int,
    *,
    transaction_id: int | None = None,
    create_transaction: bool = True,
    occurred_at: datetime | None = None,
    pay_date: date | None = None,
    overrides: dict[int, int] | None = None,
    gross_minor: int | None = None,
    tax_minor: int | None = None,
    insurance_minor: int | None = None,
) -> dict[str, Any]:
    """标记为已填写，并（可选）生成一笔入账流水。

    流水的金额用**实发**而不是应发：账户里真正到账的是实发额。
    """
    row = get_record(session, record_id)
    if row.status == "filled":
        # 幂等：重复点击不该生成第二笔流水
        return {"record_id": row.id, "transaction_id": row.transaction_id, "created": False}
    manual = any(value is not None for value in (gross_minor, tax_minor, insurance_minor))
    edited = overrides is not None or manual or pay_date is not None
    if edited and row.status != "draft":
        raise ConflictError("只有待填写的工资表可以修改", entity="payroll_record")
    items = [dict(item) for item in row.items]
    if manual:
        if items or overrides is not None or gross_minor is None:
            raise ValidationError("有组成项时请逐项填写金额；无组成项时须填写应发金额", field="gross_minor")
        for component_id, name, kind, sign, amount in (
            (0, "应发工资", "basic", 1, gross_minor),
            (-1, "个税", "tax", -1, tax_minor or 0),
            (-2, "五险一金", "insurance", -1, insurance_minor or 0),
        ):
            items.append({
                "component_id": component_id, "name": name, "kind": kind, "sign": sign,
                "direction": "add" if sign == 1 else "deduct", "calc": "manual",
                "amount_minor": amount, "rate_bps": 0, "base_key": "gross",
                "source_amount_minor": 0,
            })
    if overrides is not None:
        unknown = set(overrides) - {item["component_id"] for item in items}
        if unknown:
            raise ValidationError("金额必须对应这份工资表的组成项", field="overrides")
        for item in items:
            if item["component_id"] in overrides:
                item["amount_minor"] = overrides[item["component_id"]]
    amounts_changed = manual or overrides is not None
    if amounts_changed:
        for item in items:
            amount = item["amount_minor"]
            if type(amount) is not int or not 0 <= amount <= 9_007_199_254_740_991:
                raise ValidationError("金额须为非负整数分，且不能超出有效范围", field="amount_minor")
        gross = sum(item["amount_minor"] for item in items if item["direction"] == "add")
        deducted = sum(item["amount_minor"] for item in items if item["direction"] == "deduct")
        if gross > 9_007_199_254_740_991 or deducted > gross:
            raise ValidationError("扣减合计不能超过应发，金额不能超出有效范围", field="amount_minor")
        net = gross - deducted
    else:
        gross, net = row.gross_minor, row.net_minor
    if net <= 0 and create_transaction:
        raise ConflictError(
            "实发金额为 0，无法入账",
            entity="payroll_record",
            record_id=record_id,
            net_minor=net,
        )

    source = get_source(session, row.source_id)
    created_transaction: int | None = transaction_id
    if create_transaction and transaction_id is None:
        if source.account_id is None:
            raise ValidationError(
                "这个来源没有设置入账账户，无法生成流水", field="account_id", source_id=source.id
            )
        account = session.get(Account, source.account_id)
        if account is None or account.deleted_at is not None or account.currency != "CNY":
            raise ValidationError("请选择有效的人民币入账账户", field="account_id")
        stamp = occurred_at or datetime.combine(pay_date or row.pay_date, datetime.min.time()).replace(hour=10)
        transaction = transactions_service.create_transaction(
            session,
            type=TransactionType.INCOME.value,
            direction="in",
            account_id=source.account_id,
            category_id=source.category_id,
            amount_minor=net,
            currency="CNY",
            occurred_at=stamp,
            payee=f"{source.employer or source.name} 工资",
            note=f"{row.period} 工资（实发）",
            status="cleared",
            source=TransactionSource.MANUAL.value,
        )
        created_transaction = transaction.id

    if amounts_changed:
        row.items = items
        row.gross_minor = gross
        row.net_minor = net
        row.tax_minor = sum(item["amount_minor"] for item in items if item["kind"] == "tax" and item["direction"] == "deduct")
        row.insurance_snapshot = {"total_minor": sum(item["amount_minor"] for item in items if item["kind"] == "insurance" and item["direction"] == "deduct")}
    if pay_date is not None:
        row.pay_date = pay_date
    row.transaction_id = created_transaction
    row.status = "filled"
    row.filled_at = datetime.now()
    if created_transaction is not None and occurred_at is not None:
        row.pay_date = occurred_at.date()
    session.flush()
    return {"record_id": row.id, "transaction_id": created_transaction, "created": True}


def skip_record(session: Session, record_id: int, *, reason: str) -> PayrollRecord:
    """本月跳过。

    **必须给出原因并留痕**（PLAN 的"合规出口"设计）：
    漏填拦截如果只有"必须填"，遇到真的没有工资的月份就成了死锁，
    而用户会开始随手填假数据 —— 那比不填更糟。
    """
    cleaned = (reason or "").strip()
    if not cleaned:
        raise ValidationError("跳过必须给出原因", field="reason")
    row = get_record(session, record_id)
    if row.status == "filled":
        raise ConflictError(
            "已入账的记录不能跳过",
            entity="payroll_record",
            record_id=record_id,
            suggestion="先删除关联流水",
        )
    row.status = "skipped"
    row.skip_reason = cleaned[:200]
    session.flush()
    return row


def delete_record(session: Session, record_id: int) -> None:
    row = get_record(session, record_id)
    if row.transaction_id is not None:
        raise ConflictError(
            "这条记录已经入账，删除会让流水失去来源",
            entity="payroll_record",
            record_id=record_id,
            transaction_id=row.transaction_id,
            suggestion="先撤销入账（删除关联流水），再删除记录",
        )
    row.deleted_at = datetime.now()
    session.flush()


def list_records(
    session: Session,
    *,
    source_id: int | None = None,
    status: str | None = None,
    start_period: str | None = None,
    end_period: str | None = None,
    limit: int = 240,
) -> list[PayrollRecord]:
    statement = select(PayrollRecord).where(PayrollRecord.deleted_at.is_(None))
    if source_id is not None:
        statement = statement.where(PayrollRecord.source_id == source_id)
    if status:
        statement = statement.where(PayrollRecord.status == status)
    if start_period:
        statement = statement.where(PayrollRecord.period >= start_period)
    if end_period:
        statement = statement.where(PayrollRecord.period <= end_period)
    return list(
        session.scalars(
            statement.order_by(PayrollRecord.period.desc(), PayrollRecord.id.desc()).limit(limit)
        ).all()
    )


# -----------------------------------------------------------------------------
# 汇总与同比
# -----------------------------------------------------------------------------
def _shift_year(period: str, years: int) -> str:
    return f"{int(period[:4]) + years:04d}-{period[5:]}"


def payroll_overview(session: Session, *, period: str, history_months: int = 12) -> dict[str, Any]:
    """某一期的总览 + 同比 + 逐月趋势。

    同比用**同月去年**（工资金额有明确的年度节奏：年终奖、调薪），
    环比在这里意义不大 —— 上个月与这个月通常完全一样，
    比出来只是"没有变化"。
    """
    period = _ensure_period(period)
    # **只统计已入账的记录。** "实发"的含义是"这笔钱到账了"，
    # 而草稿还没有 —— 把草稿算进实发会让用户以为钱已经到了。
    # 草稿的数量单独返回，界面据此说明"另有 N 条未计入"。
    all_rows = list_records(session, limit=500)
    records = [row for row in all_rows if row.status == "filled"]
    draft_rows = [row for row in all_rows if row.status == "draft"]
    current = [row for row in records if row.period == period]
    current_drafts = [row for row in draft_rows if row.period == period]
    same_month_last_year = [row for row in records if row.period == _shift_year(period, -1)]

    def totals(rows: list[PayrollRecord]) -> dict[str, int]:
        return {
            "gross_minor": sum(row.gross_minor for row in rows),
            "net_minor": sum(row.net_minor for row in rows),
            "tax_minor": sum(row.tax_minor for row in rows),
            "insurance_minor": sum(int(row.insurance_snapshot.get("total_minor", 0)) for row in rows),
        }

    current_totals = totals(current)
    last_year_totals = totals(same_month_last_year)

    def delta(now: int, before: int) -> float | None:
        # 去年同月为 0 时没有可表达的百分比：返回 0 会谎称"没有变化"
        return None if before == 0 else (now - before) / abs(before)

    # 逐月趋势：按期间聚合，**补全没有记录的月份**，
    # 否则折线图会把两个相隔半年的点连成一条直线，看起来像"一直很平稳"
    buckets: dict[str, dict[str, int]] = {}
    for row in records:
        bucket = buckets.setdefault(row.period, {"gross_minor": 0, "net_minor": 0, "count": 0})
        bucket["gross_minor"] += row.gross_minor
        bucket["net_minor"] += row.net_minor
        bucket["count"] += 1

    year, month = _period_month(period)
    months: list[dict[str, Any]] = []
    cursor_year, cursor_month = year, month
    for _ in range(max(1, history_months)):
        key = f"{cursor_year:04d}-{cursor_month:02d}"
        bucket = buckets.get(key, {"gross_minor": 0, "net_minor": 0, "count": 0})
        months.append({"period": key, **bucket})
        cursor_month -= 1
        if cursor_month < 1:
            cursor_month = 12
            cursor_year -= 1
    months.reverse()

    return {
        "period": period,
        "current": {**current_totals, "count": len(current)},
        # 本期未填写的草稿条数：界面据此说明"另有 N 条未计入"，
        # 否则用户会奇怪为什么工资表里有的记录没进合计
        "draft_count": len(current_drafts),
        "same_month_last_year": {**last_year_totals, "count": len(same_month_last_year)},
        "delta": {
            "gross": delta(current_totals["gross_minor"], last_year_totals["gross_minor"]),
            "net": delta(current_totals["net_minor"], last_year_totals["net_minor"]),
            "tax": delta(current_totals["tax_minor"], last_year_totals["tax_minor"]),
            "insurance": delta(current_totals["insurance_minor"], last_year_totals["insurance_minor"]),
        },
        "months": months,
        # **必须从 draft_rows 取，不能从 records 取** ——
        # records 现在只含已入账的记录，从它里面筛草稿会永远得到空列表。
        "pending": [serialize_record(row) for row in draft_rows],
    }


def history(session: Session, *, source_id: int | None = None, limit: int = 60) -> list[dict[str, Any]]:
    return [serialize_record(row) for row in list_records(session, source_id=source_id, limit=limit)]


def serialize_record(row: PayrollRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_id": row.source_id,
        "period": row.period,
        "pay_date": row.pay_date.isoformat(),
        "gross_minor": row.gross_minor,
        "net_minor": row.net_minor,
        "tax_minor": row.tax_minor,
        "insurance_minor": int(row.insurance_snapshot.get("total_minor", 0)),
        "items": row.items,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "transaction_id": row.transaction_id,
        "filled_at": row.filled_at.isoformat() if row.filled_at else None,
        "note": row.note,
    }


def serialize_source(row: PaySource) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "employer": row.employer,
        "account_id": row.account_id,
        "category_id": row.category_id,
        "enabled": row.enabled,
        "priority": row.priority,
        "sort_order": row.sort_order,
        "note": row.note,
    }


def serialize_component(row: PayComponent) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_id": row.source_id,
        "name": row.name,
        "kind": row.kind,
        "calc": row.calc,
        "sign": row.sign,
        "amount_minor": row.amount_minor,
        "base_key": row.base_key,
        "rate_bps": row.rate_bps,
        "formula": row.formula,
        "enabled": row.enabled,
        "sort_order": row.sort_order,
    }


def upsert_payday_rule(session: Session, source_id: int, **changes: Any) -> PaydayRule:
    """设置来源的发薪规则。一个来源最多一条。"""
    get_source(session, source_id)
    rule = session.scalar(select(PaydayRule).where(PaydayRule.source_id == source_id))
    payload = {
        "day_of_month": int(changes.get("day_of_month", 15)),
        "day_kind": changes.get("day_kind", "fixed"),
        "nth": int(changes.get("nth", 1)),
        "weekend_policy": changes.get("weekend_policy", "advance"),
        "holiday_policy": changes.get("holiday_policy", "advance"),
        "enabled": bool(changes.get("enabled", True)),
        "remind_at": (changes.get("remind_at") or "09:00")[:5],
        "grace_days": int(changes.get("grace_days", 3)),
        "require_form": bool(changes.get("require_form", True)),
        "note": (changes.get("note") or "")[:200],
    }
    if payload["day_kind"] not in workdays.DAY_KINDS:
        raise ValidationError("未知的发薪日类型", field="day_kind", allowed=list(workdays.DAY_KINDS))
    for key in ("weekend_policy", "holiday_policy"):
        if payload[key] not in workdays.POLICIES:
            raise ValidationError(
                f"未知的调整策略：{payload[key]}", field=key, allowed=list(workdays.POLICIES)
            )
    if not 1 <= payload["day_of_month"] <= 31:
        raise ValidationError("发薪日必须在 1–31 之间", field="day_of_month")
    if not 0 <= payload["grace_days"] <= 30:
        raise ValidationError("宽限天数必须在 0–30 之间", field="grace_days")

    if rule is None:
        rule = PaydayRule(source_id=source_id, **payload)
        session.add(rule)
    else:
        for key, value in payload.items():
            setattr(rule, key, value)
    session.flush()
    return rule


def get_payday_rule(session: Session, source_id: int) -> PaydayRule | None:
    get_source(session, source_id)
    return session.scalar(select(PaydayRule).where(PaydayRule.source_id == source_id))


def serialize_rule(rule: PaydayRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "id": rule.id,
        "source_id": rule.source_id,
        "day_of_month": rule.day_of_month,
        "day_kind": rule.day_kind,
        "nth": rule.nth,
        "weekend_policy": rule.weekend_policy,
        "holiday_policy": rule.holiday_policy,
        "enabled": rule.enabled,
        "remind_at": rule.remind_at,
        "grace_days": rule.grace_days,
        "require_form": rule.require_form,
        "note": rule.note,
    }


def upcoming(session: Session, *, start: date | None = None, months: int = 3) -> list[dict[str, Any]]:
    """未来发薪日，跨来源合并排序，并补上来源名。"""
    start = start or datetime.now().date()
    rules = list(session.scalars(select(PaydayRule).where(PaydayRule.enabled.is_(True))).all())
    sources = {row.id: row for row in list_sources(session)}
    results = workdays.upcoming_paydays(session, rules, start=start, months=months)
    for item in results:
        source = sources.get(int(item["source_id"])) if item.get("source_id") else None
        item["source_name"] = source.name if source else ""
        item["employer"] = source.employer if source else ""
    return results


def pending_records(session: Session, *, grace_days: int = 0) -> list[dict[str, Any]]:
    """待填写且已经过了宽限期的记录 —— 供"漏填拦截"使用。

    **只返回真的过了宽限期的**：发薪当天就弹窗拦截是把提醒做成了骚扰。
    """
    today = datetime.now().date()
    rows = session.scalars(
        select(PayrollRecord).where(PayrollRecord.deleted_at.is_(None), PayrollRecord.status == "draft")
    ).all()
    results: list[dict[str, Any]] = []
    for row in rows:
        rule = session.scalar(select(PaydayRule).where(PaydayRule.source_id == row.source_id))
        grace = int(rule.grace_days) if rule is not None else grace_days
        overdue_days = (today - row.pay_date).days - grace
        if overdue_days < 0:
            continue
        results.append(
            {
                **serialize_record(row),
                "overdue_days": overdue_days,
                "require_form": bool(rule.require_form) if rule is not None else True,
                "caught_up": True,
            }
        )
    results.sort(key=lambda item: (-int(item["overdue_days"]), str(item["period"])))
    return results


def ensure_record_for_period(session: Session, source_id: int, period: str) -> PayrollRecord:
    """确保某来源某期间有一条草稿记录（幂等）。

    调度器在发薪日调用它：**已经存在就返回现有的**，
    不会覆盖用户已经改过的金额。
    """
    period = _ensure_period(period)
    existing = session.scalar(
        select(PayrollRecord).where(
            PayrollRecord.source_id == source_id,
            PayrollRecord.period == period,
            PayrollRecord.deleted_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    return create_record(session, source_id, period)


def _month_range(period: str) -> tuple[date, date]:
    year, month = _period_month(period)
    start = date(year, month, 1)
    end = start + timedelta(days=32)
    return start, end.replace(day=1) - timedelta(days=1)


def month_bounds(period: str) -> tuple[date, date]:
    """期间的起止日。报表的 payroll 一节用它把记录框进区间。"""
    return _month_range(_ensure_period(period))
