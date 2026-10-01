"""五险一金（P6 / 需求 18）。

险种名称是事实，比例不是
========================
"五险一金"包含哪些险种、哪些**个人不缴**（工伤、生育）、
哪些**进入个人账户**（养老个人部分、医疗个人部分、公积金的双方部分）
—— 这些都是制度性事实，可以写进字典。

但**比例因城市、因年份而异**：同一险种在不同城市的比例不同，
封顶基数每年随社平工资调整，还有地方性的补充公积金与企业年金。
写死一套比例等于给用户一个**看起来权威、实际只对某市某年成立**的数字，
而他不会去核对。因此 `ensure_standard_items()` 只铺**名称与结构标志**，
比例一律留 0，界面提示"请按当地政策填写"。

这与"法定节假日日期"是同一类问题，处理方式也相同。

余额也不落库
============
`余额 = Σ计入个人账户的缴纳 + Σ利息 − Σ提取`。
利息需要利率，而利率同样由用户录在年度对账里 ——
这样余额能对得上，而我**不必编造任何利率**。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..db.models import (
    InsuranceAnnualStatement,
    InsuranceContribution,
    InsuranceItem,
    InsuranceProfile,
    InsuranceWithdrawal,
    Member,
)

__all__ = [
    "STANDARD_ITEMS",
    "WITHDRAWAL_REASONS",
    "account_balances",
    "add_profile",
    "add_withdrawal",
    "annual_statement",
    "compute_contribution",
    "delete_item",
    "delete_profile",
    "delete_withdrawal",
    "ensure_standard_items",
    "get_item",
    "get_profile",
    "insurance_overview",
    "list_contributions",
    "list_items",
    "list_profiles",
    "list_withdrawals",
    "record_contribution",
    "restore_profile",
    "serialize_contribution",
    "serialize_item",
    "serialize_profile",
    "serialize_withdrawal",
    "upsert_item",
    "upsert_statement",
]

_logger = logging.getLogger(__name__)

#: 提取原因
WITHDRAWAL_REASONS = (
    "purchase",
    "rent",
    "retirement",
    "medical",
    "settlement",
    "other",
)

#: 标准险种。**只给名称与结构标志，比例一律 0。**
#:
#: `personal_to_account` / `employer_to_account` 是制度性事实：
#: 养老与医疗的**个人**部分进个人账户；公积金的**双方**部分都进个人账户；
#: 工伤与生育**个人不缴**。
STANDARD_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "kind": "pension",
        "name": "养老保险",
        "personal_to_account": True,
        "employer_to_account": False,
        "note": "个人部分计入个人账户；单位部分进统筹",
    },
    {
        "kind": "medical",
        "name": "医疗保险",
        "personal_to_account": True,
        "employer_to_account": False,
        "note": "个人部分计入医保个人账户",
    },
    {
        "kind": "unemployment",
        "name": "失业保险",
        "personal_to_account": False,
        "employer_to_account": False,
    },
    {
        "kind": "injury",
        "name": "工伤保险",
        "personal_to_account": False,
        "employer_to_account": False,
        "note": "个人不缴（制度如此，不是未填写）",
    },
    {
        "kind": "maternity",
        "name": "生育保险",
        "personal_to_account": False,
        "employer_to_account": False,
        "note": "个人不缴（部分城市已并入医疗）",
    },
    {
        "kind": "housing_fund",
        "name": "住房公积金",
        "personal_to_account": True,
        "employer_to_account": True,
        "use_housing_base": True,
        "note": "个人与单位缴纳均计入个人公积金账户",
    },
    {
        "kind": "supplementary_fund",
        "name": "补充公积金",
        "personal_to_account": True,
        "employer_to_account": True,
        "use_housing_base": True,
    },
    {
        "kind": "enterprise_annuity",
        "name": "企业年金",
        "personal_to_account": True,
        "employer_to_account": True,
    },
    {
        "kind": "critical_illness",
        "name": "大病医疗",
        "personal_to_account": False,
        "employer_to_account": False,
    },
)


def _q(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# -----------------------------------------------------------------------------
# 字典
# -----------------------------------------------------------------------------
def ensure_standard_items(session: Session, *, city: str = "") -> list[InsuranceItem]:
    """铺标准险种（**幂等**，只补缺失的名称，不覆盖用户已填的比例）。

    这一条很要紧：用户填完比例之后再调用它，不能把比例清掉。
    """
    existing = {
        row.kind: row
        for row in session.scalars(select(InsuranceItem).where(InsuranceItem.city == city)).all()
    }
    created: list[InsuranceItem] = []
    for index, spec in enumerate(STANDARD_ITEMS):
        if spec["kind"] in existing:
            continue
        row = InsuranceItem(
            kind=spec["kind"],
            name=spec["name"],
            city=city,
            personal_to_account=bool(spec.get("personal_to_account", False)),
            employer_to_account=bool(spec.get("employer_to_account", False)),
            use_housing_base=bool(spec.get("use_housing_base", False)),
            note=str(spec.get("note", "")),
            sort_order=index,
        )
        session.add(row)
        created.append(row)
    session.flush()
    return list_items(session, city=city)


def list_items(
    session: Session, *, city: str | None = None, include_disabled: bool = False
) -> list[InsuranceItem]:
    statement = select(InsuranceItem)
    if city is not None:
        # 城市专属项 + 通用项（city 为空）
        statement = statement.where((InsuranceItem.city == city) | (InsuranceItem.city == ""))
    if not include_disabled:
        statement = statement.where(InsuranceItem.enabled.is_(True))
    return list(session.scalars(statement.order_by(InsuranceItem.sort_order, InsuranceItem.id)).all())


def get_item(session: Session, item_id: int) -> InsuranceItem:
    row = session.get(InsuranceItem, item_id)
    if row is None:
        raise NotFoundError("险种不存在", entity="insurance_item", entity_id=item_id)
    return row


def upsert_item(session: Session, *, kind: str, city: str = "", **changes: Any) -> InsuranceItem:
    """按 (kind, city) 建立或更新一个险种。"""
    if kind not in {spec["kind"] for spec in STANDARD_ITEMS} and kind != "other":
        raise ValidationError(
            f"未知的险种：{kind}",
            field="kind",
            allowed=sorted({spec["kind"] for spec in STANDARD_ITEMS} | {"other"}),
        )
    for key in ("personal_rate_bps", "employer_rate_bps"):
        if key in changes and changes[key] is not None and not 0 <= int(changes[key]) <= 10000:
            raise ValidationError("比例必须在 0–100% 之间", field=key)
    for key in ("floor_base_minor", "cap_base_minor"):
        if key in changes and changes[key] is not None and int(changes[key]) < 0:
            raise ValidationError("基数不能为负", field=key)
    floor = changes.get("floor_base_minor")
    cap = changes.get("cap_base_minor")
    if floor is not None and cap is not None and int(floor) > int(cap):
        raise ValidationError("保底基数不能高于封顶基数", field="floor_base_minor")

    row = session.scalar(select(InsuranceItem).where(InsuranceItem.kind == kind, InsuranceItem.city == city))
    if row is None:
        defaults = next((spec for spec in STANDARD_ITEMS if spec["kind"] == kind), {})
        row = InsuranceItem(
            kind=kind,
            city=city,
            name=str(changes.get("name") or defaults.get("name") or kind),
            personal_to_account=bool(defaults.get("personal_to_account", False)),
            employer_to_account=bool(defaults.get("employer_to_account", False)),
            use_housing_base=bool(defaults.get("use_housing_base", False)),
        )
        session.add(row)
    for key in (
        "name",
        "personal_rate_bps",
        "employer_rate_bps",
        "personal_to_account",
        "employer_to_account",
        "floor_base_minor",
        "cap_base_minor",
        "use_housing_base",
        "enabled",
        "sort_order",
        "note",
    ):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    session.flush()
    return row


def delete_item(session: Session, item_id: int) -> None:
    row = get_item(session, item_id)
    used = session.scalar(
        select(func.count(InsuranceContribution.id)).where(InsuranceContribution.item_id == item_id)
    )
    if used:
        # 删掉会让历史缴纳记录失去险种 —— 余额与年度对账都会跟着错
        raise ConflictError(
            "这个险种已经有缴纳记录，不能删除",
            entity="insurance_item",
            entity_id=item_id,
            contribution_count=int(used),
            suggestion="把它停用（enabled=false），历史记录会保留",
        )
    session.delete(row)
    session.flush()


def serialize_item(row: InsuranceItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "city": row.city,
        "personal_rate_bps": row.personal_rate_bps,
        "employer_rate_bps": row.employer_rate_bps,
        "personal_to_account": row.personal_to_account,
        "employer_to_account": row.employer_to_account,
        "floor_base_minor": row.floor_base_minor,
        "cap_base_minor": row.cap_base_minor,
        "use_housing_base": row.use_housing_base,
        "enabled": row.enabled,
        "sort_order": row.sort_order,
        "note": row.note,
        # 界面上要能直接看出"这一项还没填比例"，而不是显示成 0%
        "rates_filled": bool(row.personal_rate_bps or row.employer_rate_bps),
    }


# -----------------------------------------------------------------------------
# 档案
# -----------------------------------------------------------------------------
def add_profile(
    session: Session,
    *,
    name: str,
    member_id: int | None = None,
    city: str = "",
    employer: str = "",
    social_base_minor: int = 0,
    housing_base_minor: int = 0,
    effective_from: date | None = None,
    effective_to: date | None = None,
    note: str = "",
) -> InsuranceProfile:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("档案名称不能为空", field="name")
    if social_base_minor < 0 or housing_base_minor < 0:
        raise ValidationError("基数不能为负", field="social_base_minor")
    if effective_from and effective_to and effective_to < effective_from:
        raise ValidationError("结束日期不能早于开始日期", field="effective_to")
    if member_id is not None and session.get(Member, member_id) is None:
        raise NotFoundError("成员不存在", entity="member", entity_id=member_id)
    row = InsuranceProfile(
        name=cleaned,
        member_id=member_id,
        city=(city or "").strip(),
        employer=(employer or "").strip(),
        social_base_minor=int(social_base_minor),
        housing_base_minor=int(housing_base_minor),
        effective_from=effective_from,
        effective_to=effective_to,
        note=(note or "").strip(),
    )
    session.add(row)
    session.flush()
    return row


def get_profile(session: Session, profile_id: int) -> InsuranceProfile:
    row = session.get(InsuranceProfile, profile_id)
    if row is None or row.deleted_at is not None:
        raise NotFoundError("参保档案不存在", entity="insurance_profile", entity_id=profile_id)
    return row


def list_profiles(session: Session, *, include_disabled: bool = False) -> list[InsuranceProfile]:
    statement = select(InsuranceProfile).where(InsuranceProfile.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(InsuranceProfile.enabled.is_(True))
    return list(session.scalars(statement.order_by(InsuranceProfile.id)).all())


def update_profile(session: Session, profile_id: int, **changes: Any) -> InsuranceProfile:
    row = get_profile(session, profile_id)
    for key in (
        "name",
        "member_id",
        "city",
        "employer",
        "social_base_minor",
        "housing_base_minor",
        "effective_from",
        "effective_to",
        "enabled",
        "note",
    ):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    session.flush()
    return row


def delete_profile(session: Session, profile_id: int) -> None:
    row = get_profile(session, profile_id)
    row.deleted_at = datetime.now()
    session.flush()


def restore_profile(session: Session, profile_id: int) -> InsuranceProfile:
    row = session.get(InsuranceProfile, profile_id)
    if row is None:
        raise NotFoundError("参保档案不存在", entity="insurance_profile", entity_id=profile_id)
    row.deleted_at = None
    session.flush()
    return row


def serialize_profile(session: Session, row: InsuranceProfile) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "member_id": row.member_id,
        "city": row.city,
        "employer": row.employer,
        "social_base_minor": row.social_base_minor,
        "housing_base_minor": row.housing_base_minor,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "enabled": row.enabled,
        "note": row.note,
        "account_balance_minor": sum(account_balances(session, profile_id=row.id).values()),
    }


# -----------------------------------------------------------------------------
# 缴纳计算
# -----------------------------------------------------------------------------
def _base_for(item: InsuranceItem, profile: InsuranceProfile) -> int:
    return int(profile.housing_base_minor if item.use_housing_base else profile.social_base_minor)


def clamp_base(item: InsuranceItem, raw: int) -> tuple[int, str]:
    """按保底/封顶收敛基数。返回 ``(实际基数, 'floor'|'cap'|'')``。

    收敛**必须报出来**：用户填了 3 万基数、系统按 2.4 万算，
    如果不说明，他会以为软件算错了。
    """
    base = raw
    reason = ""
    if item.floor_base_minor is not None and base < int(item.floor_base_minor):
        base = int(item.floor_base_minor)
        reason = "floor"
    if item.cap_base_minor is not None and base > int(item.cap_base_minor):
        base = int(item.cap_base_minor)
        reason = "cap"
    return base, reason


def compute_contribution(session: Session, profile_id: int, *, city: str | None = None) -> dict[str, Any]:
    """按档案与险种字典算出逐项缴纳额。

    与工资表一样走 `Decimal` 并**最后一次性取整**：
    逐项逐步取整会让"个人与单位合计"与官方对账单差几分钱。
    """
    profile = get_profile(session, profile_id)
    items = list_items(session, city=city if city is not None else profile.city)
    payload: list[dict[str, Any]] = []
    for item in items:
        raw = _base_for(item, profile)
        base, clamp_reason = clamp_base(item, raw)
        personal = _q(Decimal(base) * Decimal(item.personal_rate_bps) / Decimal(10000))
        employer = _q(Decimal(base) * Decimal(item.employer_rate_bps) / Decimal(10000))
        to_account = 0
        if item.personal_to_account:
            to_account += personal
        if item.employer_to_account:
            to_account += employer
        payload.append(
            {
                "item_id": item.id,
                "kind": item.kind,
                "name": item.name,
                "raw_base_minor": raw,
                "base_minor": base,
                "clamped": clamp_reason,
                "personal_rate_bps": item.personal_rate_bps,
                "employer_rate_bps": item.employer_rate_bps,
                "personal_minor": personal,
                "employer_minor": employer,
                "to_account_minor": to_account,
                "rates_filled": bool(item.personal_rate_bps or item.employer_rate_bps),
            }
        )
    return {
        "profile_id": profile_id,
        "profile_name": profile.name,
        "city": profile.city,
        "items": payload,
        "personal_total_minor": sum(item["personal_minor"] for item in payload),
        "employer_total_minor": sum(item["employer_minor"] for item in payload),
        "to_account_total_minor": sum(item["to_account_minor"] for item in payload),
        # 有险种还没填比例时，合计必然偏小 —— 必须让调用方知道。
        # **一项都没有时同样算不完整**：那说明字典还没铺，
        # 而"0 元合计"在界面上与"这个月没缴"长得一模一样。
        "incomplete": (not payload) or any(not item["rates_filled"] for item in payload),
        "unfilled_items": [item["name"] for item in payload if not item["rates_filled"]],
    }


def record_contribution(
    session: Session,
    profile_id: int,
    period: str,
    *,
    source: str = "manual",
    payroll_record_id: int | None = None,
    overwrite: bool = False,
) -> list[InsuranceContribution]:
    """把某一期的缴纳写入 `insurance_contributions`。

    **幂等**：同一 (档案, 期间, 险种) 已存在时不重复写入，
    除非显式要求 `overwrite`。调度器与工资收录都可能触发它，
    重复写入会让余额凭空翻倍。
    """
    period = _ensure_period(period)
    computed = compute_contribution(session, profile_id)
    rows: list[InsuranceContribution] = []
    for item in computed["items"]:
        existing = session.scalar(
            select(InsuranceContribution).where(
                InsuranceContribution.profile_id == profile_id,
                InsuranceContribution.period == period,
                InsuranceContribution.item_id == item["item_id"],
            )
        )
        if existing is not None and not overwrite:
            rows.append(existing)
            continue
        if existing is None:
            existing = InsuranceContribution(profile_id=profile_id, item_id=item["item_id"], period=period)
            session.add(existing)
        existing.base_minor = item["base_minor"]
        existing.raw_base_minor = item["raw_base_minor"]
        existing.personal_rate_bps = item["personal_rate_bps"]
        existing.employer_rate_bps = item["employer_rate_bps"]
        existing.personal_minor = item["personal_minor"]
        existing.employer_minor = item["employer_minor"]
        existing.to_account_minor = item["to_account_minor"]
        existing.source = source
        existing.payroll_record_id = payroll_record_id
        rows.append(existing)
    session.flush()
    return rows


def _ensure_period(period: str) -> str:
    text = (period or "").strip()
    if len(text) != 7 or text[4] != "-":
        raise ValidationError("期间格式应为 YYYY-MM", field="period", got=text)
    try:
        year, month = int(text[:4]), int(text[5:])
    except ValueError as error:
        raise ValidationError("期间格式应为 YYYY-MM", field="period", got=text) from error
    if not 1 <= month <= 12:
        raise ValidationError("月份必须在 1–12 之间", field="period")
    return f"{year:04d}-{month:02d}"


def list_contributions(
    session: Session,
    *,
    profile_id: int | None = None,
    period: str | None = None,
    start_period: str | None = None,
    end_period: str | None = None,
    limit: int = 600,
) -> list[InsuranceContribution]:
    statement = select(InsuranceContribution)
    if profile_id is not None:
        statement = statement.where(InsuranceContribution.profile_id == profile_id)
    if period:
        statement = statement.where(InsuranceContribution.period == period)
    if start_period:
        statement = statement.where(InsuranceContribution.period >= start_period)
    if end_period:
        statement = statement.where(InsuranceContribution.period <= end_period)
    return list(
        session.scalars(
            statement.order_by(InsuranceContribution.period.desc(), InsuranceContribution.item_id).limit(
                limit
            )
        ).all()
    )


def serialize_contribution(session: Session, row: InsuranceContribution) -> dict[str, Any]:
    item = session.get(InsuranceItem, row.item_id)
    return {
        "id": row.id,
        "profile_id": row.profile_id,
        "item_id": row.item_id,
        "item_name": item.name if item else "",
        "item_kind": item.kind if item else "",
        "period": row.period,
        "base_minor": row.base_minor,
        "raw_base_minor": row.raw_base_minor,
        "personal_rate_bps": row.personal_rate_bps,
        "employer_rate_bps": row.employer_rate_bps,
        "personal_minor": row.personal_minor,
        "employer_minor": row.employer_minor,
        "to_account_minor": row.to_account_minor,
        "source": row.source,
        "payroll_record_id": row.payroll_record_id,
        "transaction_id": row.transaction_id,
        "note": row.note,
    }


# -----------------------------------------------------------------------------
# 个人账户余额（派生）
# -----------------------------------------------------------------------------
def account_balances(session: Session, *, profile_id: int | None = None) -> dict[str, int]:
    """按**险种类型**汇总的个人账户余额。**算出来，不落库。**

    `余额 = Σ计入个人账户的缴纳 + Σ利息 − Σ提取`

    利息来自年度对账里用户录入的数（利率因城市与年份而异，我不编）。
    """
    conditions = []
    if profile_id is not None:
        conditions.append(InsuranceContribution.profile_id == profile_id)

    credited_rows = session.execute(
        select(InsuranceItem.kind, func.coalesce(func.sum(InsuranceContribution.to_account_minor), 0))
        .join(InsuranceItem, InsuranceItem.id == InsuranceContribution.item_id)
        .where(*conditions, InsuranceContribution.to_account_minor > 0)
        .group_by(InsuranceItem.kind)
    ).all()
    balances: dict[str, int] = {str(row[0]): int(row[1]) for row in credited_rows}

    # 利息按档案计入（不细分到险种：对账单上的利息通常是账户级的）
    interest_conditions = []
    if profile_id is not None:
        interest_conditions.append(InsuranceAnnualStatement.profile_id == profile_id)
    interest = session.scalar(
        select(func.coalesce(func.sum(InsuranceAnnualStatement.interest_minor), 0)).where(
            *interest_conditions
        )
    )
    if interest:
        # 利息归到公积金与养老两个最常见的账户；没有明细时记为 total
        balances["total_interest"] = int(interest)

    withdraw_conditions = []
    if profile_id is not None:
        withdraw_conditions.append(InsuranceWithdrawal.profile_id == profile_id)
    withdrawn_rows = session.execute(
        select(InsuranceItem.kind, func.coalesce(func.sum(InsuranceWithdrawal.amount_minor), 0))
        .join(InsuranceItem, InsuranceItem.id == InsuranceWithdrawal.item_id)
        .where(*withdraw_conditions)
        .group_by(InsuranceItem.kind)
    ).all()
    for kind, amount in withdrawn_rows:
        balances[str(kind)] = balances.get(str(kind), 0) - int(amount)

    if "total_interest" in balances:
        # 把利息并进"最常见的有账户的险种"里，而不是留一个游离的键
        interest_amount = balances.pop("total_interest")
        target = "housing_fund" if "housing_fund" in balances else "pension"
        balances[target] = balances.get(target, 0) + interest_amount
    return balances


# -----------------------------------------------------------------------------
# 提取
# -----------------------------------------------------------------------------
def add_withdrawal(
    session: Session,
    profile_id: int,
    *,
    item_id: int,
    amount_minor: int,
    occurred_at: date,
    reason: str = "other",
    transaction_id: int | None = None,
    note: str = "",
) -> InsuranceWithdrawal:
    get_profile(session, profile_id)
    item = get_item(session, item_id)
    if amount_minor <= 0:
        raise ValidationError("提取金额必须大于 0", field="amount_minor")
    if reason not in WITHDRAWAL_REASONS:
        raise ValidationError(f"未知的提取原因：{reason}", field="reason", allowed=list(WITHDRAWAL_REASONS))
    balances = account_balances(session, profile_id=profile_id)
    available = balances.get(item.kind, 0)
    if available and amount_minor > available:
        # 余额是算出来的，提超了会让余额变成负数 —— 那不是一个有意义的账户状态
        raise ConflictError(
            f"「{item.name}」账户余额不足",
            entity="insurance_withdrawal",
            available_minor=available,
            requested_minor=amount_minor,
            suggestion="先核对历史缴纳记录，或把金额改小",
        )
    row = InsuranceWithdrawal(
        profile_id=profile_id,
        item_id=item_id,
        amount_minor=int(amount_minor),
        occurred_at=occurred_at,
        reason=reason,
        transaction_id=transaction_id,
        note=(note or "").strip(),
    )
    session.add(row)
    session.flush()
    return row


def list_withdrawals(
    session: Session, *, profile_id: int | None = None, limit: int = 200
) -> list[InsuranceWithdrawal]:
    statement = select(InsuranceWithdrawal)
    if profile_id is not None:
        statement = statement.where(InsuranceWithdrawal.profile_id == profile_id)
    return list(
        session.scalars(statement.order_by(InsuranceWithdrawal.occurred_at.desc()).limit(limit)).all()
    )


def delete_withdrawal(session: Session, withdrawal_id: int) -> None:
    row = session.get(InsuranceWithdrawal, withdrawal_id)
    if row is None:
        raise NotFoundError("提取记录不存在", entity="insurance_withdrawal", entity_id=withdrawal_id)
    session.delete(row)
    session.flush()


def serialize_withdrawal(session: Session, row: InsuranceWithdrawal) -> dict[str, Any]:
    item = session.get(InsuranceItem, row.item_id)
    return {
        "id": row.id,
        "profile_id": row.profile_id,
        "item_id": row.item_id,
        "item_name": item.name if item else "",
        "amount_minor": row.amount_minor,
        "occurred_at": row.occurred_at.isoformat(),
        "reason": row.reason,
        "transaction_id": row.transaction_id,
        "note": row.note,
    }


# -----------------------------------------------------------------------------
# 年度对账
# -----------------------------------------------------------------------------
def upsert_statement(
    session: Session,
    profile_id: int,
    year: int,
    *,
    expected_personal_minor: int | None = None,
    expected_employer_minor: int | None = None,
    expected_balance_minor: int | None = None,
    interest_minor: int = 0,
    note: str = "",
) -> InsuranceAnnualStatement:
    get_profile(session, profile_id)
    if not 1900 <= int(year) <= 2200:
        raise ValidationError("年份超出合理范围", field="year")
    row = session.scalar(
        select(InsuranceAnnualStatement).where(
            InsuranceAnnualStatement.profile_id == profile_id,
            InsuranceAnnualStatement.year == int(year),
        )
    )
    if row is None:
        row = InsuranceAnnualStatement(profile_id=profile_id, year=int(year))
        session.add(row)
    row.expected_personal_minor = expected_personal_minor
    row.expected_employer_minor = expected_employer_minor
    row.expected_balance_minor = expected_balance_minor
    row.interest_minor = int(interest_minor)
    row.note = (note or "")[:200]
    if any(
        value is not None
        for value in (expected_personal_minor, expected_employer_minor, expected_balance_minor)
    ):
        row.reconciled_at = datetime.now()
    session.flush()
    return row


def annual_statement(session: Session, profile_id: int, year: int) -> dict[str, Any]:
    """年度对账：系统算出的合计 vs 官方对账单上的数，给出差异。

    **差异是这份报表的全部意义**：它要么证明记录是准的，
    要么指出"有几个月没记 / 有几个月记重了"。
    因此差异为 0 时也要显式写出来，而不是留空。
    """
    get_profile(session, profile_id)
    start, end = f"{int(year):04d}-01", f"{int(year):04d}-12"
    rows = list_contributions(session, profile_id=profile_id, start_period=start, end_period=end, limit=2000)
    by_item: dict[int, dict[str, Any]] = {}
    for row in rows:
        bucket = by_item.setdefault(
            row.item_id,
            {"personal_minor": 0, "employer_minor": 0, "to_account_minor": 0, "months": 0},
        )
        bucket["personal_minor"] += row.personal_minor
        bucket["employer_minor"] += row.employer_minor
        bucket["to_account_minor"] += row.to_account_minor
        bucket["months"] += 1

    items = []
    for item_id, bucket in by_item.items():
        item = session.get(InsuranceItem, item_id)
        items.append(
            {
                "item_id": item_id,
                "item_name": item.name if item else "",
                "item_kind": item.kind if item else "",
                **bucket,
            }
        )
    items.sort(key=lambda entry: -int(entry["personal_minor"]))

    computed_personal = sum(item["personal_minor"] for item in items)
    computed_employer = sum(item["employer_minor"] for item in items)

    statement = session.scalar(
        select(InsuranceAnnualStatement).where(
            InsuranceAnnualStatement.profile_id == profile_id,
            InsuranceAnnualStatement.year == int(year),
        )
    )
    balances = account_balances(session, profile_id=profile_id)

    def difference(actual: int, expected: int | None) -> int | None:
        return None if expected is None else actual - int(expected)

    return {
        "profile_id": profile_id,
        "year": int(year),
        "months_recorded": len({row.period for row in rows}),
        "items": items,
        "computed_personal_minor": computed_personal,
        "computed_employer_minor": computed_employer,
        "computed_total_minor": computed_personal + computed_employer,
        "interest_minor": int(statement.interest_minor) if statement else 0,
        "account_balances": balances,
        "account_total_minor": sum(balances.values()),
        "expected": {
            "personal_minor": statement.expected_personal_minor if statement else None,
            "employer_minor": statement.expected_employer_minor if statement else None,
            "balance_minor": statement.expected_balance_minor if statement else None,
        },
        "difference": {
            "personal_minor": difference(
                computed_personal, statement.expected_personal_minor if statement else None
            ),
            "employer_minor": difference(
                computed_employer, statement.expected_employer_minor if statement else None
            ),
            "balance_minor": difference(
                sum(balances.values()),
                statement.expected_balance_minor if statement else None,
            ),
        },
        # 有月份没记录时，差异很可能来自这里 —— 直接说明，省得用户自己猜
        "expected_months": 12,
        "missing_months": max(0, 12 - len({row.period for row in rows})),
        "reconciled_at": statement.reconciled_at.isoformat()
        if statement and statement.reconciled_at
        else None,
    }


# -----------------------------------------------------------------------------
# 总览（报表用）
# -----------------------------------------------------------------------------
def insurance_overview(session: Session, *, start_period: str, end_period: str) -> dict[str, Any]:
    """区间内的五险一金总览：个人/单位合计、构成、账户余额、逐年累积。"""
    rows = list_contributions(session, start_period=start_period, end_period=end_period, limit=5000)
    by_kind: dict[str, dict[str, Any]] = {}
    by_period: dict[str, dict[str, int]] = {}
    for row in rows:
        item = session.get(InsuranceItem, row.item_id)
        kind = item.kind if item else "other"
        name = item.name if item else ""
        bucket = by_kind.setdefault(
            kind, {"kind": kind, "name": name, "personal_minor": 0, "employer_minor": 0}
        )
        bucket["personal_minor"] += row.personal_minor
        bucket["employer_minor"] += row.employer_minor
        period_bucket = by_period.setdefault(row.period, {"personal_minor": 0, "employer_minor": 0})
        period_bucket["personal_minor"] += row.personal_minor
        period_bucket["employer_minor"] += row.employer_minor

    personal = sum(bucket["personal_minor"] for bucket in by_kind.values())
    employer = sum(bucket["employer_minor"] for bucket in by_kind.values())
    profiles = list_profiles(session, include_disabled=True)
    balances: dict[str, int] = {}
    for profile in profiles:
        for kind, amount in account_balances(session, profile_id=profile.id).items():
            balances[kind] = balances.get(kind, 0) + amount

    return {
        "start_period": start_period,
        "end_period": end_period,
        "personal_total_minor": personal,
        "employer_total_minor": employer,
        "total_minor": personal + employer,
        # 单位缴纳是"隐形收入"：它没进工资卡，但确实是你的
        "employer_share": (employer / (personal + employer)) if (personal + employer) else None,
        "by_kind": sorted(
            by_kind.values(), key=lambda item: -(item["personal_minor"] + item["employer_minor"])
        ),
        "by_period": [{"period": key, **value} for key, value in sorted(by_period.items())],
        "account_balances": balances,
        "account_total_minor": sum(balances.values()),
        "profile_count": len(profiles),
        "record_count": len(rows),
    }
