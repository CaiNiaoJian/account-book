"""账户服务 —— 账户 CRUD 与**余额聚合**。

余额为什么算而不是存
--------------------
``accounts`` 表里没有 ``balance_minor`` 列。余额 = 起点余额 + 全部未删除流水的
净影响，每次查询时聚合。理由：

* 冗余余额在"导入历史流水 / 手工改单 / 进程崩溃"时极易与流水不一致，
  而一旦不一致，用户会看到"余额对不上账"却无从修复；
* SQLite 在几万条流水规模下的聚合是毫秒级，实测完全够用；
* 若将来数据量真的到了瓶颈，再加带版本号的物化余额表 —— 那时也知道该优化什么。

余额公式（无分支，靠 ``direction`` 与转账双端表达）
--------------------------------------------------
对账户 A：

    Σ(account_id = A 且 direction='in'  的 amount)
  - Σ(account_id = A 且 direction='out' 的 amount)
  + Σ(to_account_id = A 的 amount)          ← 转入

排除 ``deleted_at IS NOT NULL`` 与 ``status='void'`` 的流水。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.domain import AccountType
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.money import DEFAULT_CURRENCY
from ..db.models import Account, Transaction
from . import audit

__all__ = [
    "ACCOUNT_MUTABLE_FIELDS",
    "LIABILITY_TYPES",
    "account_balance",
    "balances_by_account",
    "create_account",
    "delete_account",
    "get_account",
    "list_accounts",
    "overview",
    "restore_account",
    "update_account",
]

_logger = logging.getLogger(__name__)

#: 可被创建/修改的字段白名单。
#: 用白名单而不是 ``setattr`` 任意字段：API 传来的字典直接落到 ORM 上，
#: 是"越权改字段"这类漏洞最经典的入口。
ACCOUNT_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "type",
        "currency",
        "initial_balance_minor",
        "icon",
        "color",
        "institution",
        "card_no_tail",
        "credit_limit_minor",
        "bill_day",
        "due_day",
        "include_in_net_worth",
        "is_archived",
        "sort_order",
        "note",
        "meta",
    }
)

#: 负债性质的账户类型 —— 仅用于界面分组与提示，
#: 净值统计实际按"余额正负"归类（见 :func:`overview`），对用户更直观。
LIABILITY_TYPES: frozenset[str] = frozenset({AccountType.CREDIT_CARD.value, AccountType.PAYABLE.value})


# -----------------------------------------------------------------------------
# 查询
# -----------------------------------------------------------------------------
def list_accounts(
    session: Session,
    *,
    include_archived: bool = False,
    include_deleted: bool = False,
) -> list[Account]:
    """按排序返回账户。默认隐藏已归档与被删除的账户。"""
    statement = select(Account)
    if not include_deleted:
        statement = statement.where(Account.deleted_at.is_(None))
    if not include_archived:
        statement = statement.where(Account.is_archived.is_(False))
    statement = statement.order_by(Account.sort_order, Account.id)
    return list(session.scalars(statement).all())


def get_account(session: Session, account_id: int, *, include_deleted: bool = False) -> Account:
    """按 id 取账户；不存在（或已软删除）时抛 :class:`NotFoundError`。"""
    account = session.get(Account, account_id)
    if account is None or (account.is_deleted and not include_deleted):
        raise NotFoundError("账户不存在或已被删除", entity="account", entity_id=account_id)
    return account


def _assert_name_available(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    """校验账户名未被占用（软删除的不算）。

    数据库有部分唯一索引兜底，但这里提前给出**可读的领域错误** ——
    让用户看到"已有同名账户"而不是一条 SQLite 约束名。
    """
    statement = select(Account.id).where(Account.name == name, Account.deleted_at.is_(None))
    if exclude_id is not None:
        statement = statement.where(Account.id != exclude_id)
    if session.scalar(statement.limit(1)) is not None:
        raise ConflictError(f"已存在同名账户：{name}", field="name", value=name)


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def create_account(session: Session, **fields: Any) -> Account:
    """创建账户。只接受 :data:`ACCOUNT_MUTABLE_FIELDS` 中的字段。"""
    unknown = set(fields) - ACCOUNT_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的账户字段：{sorted(unknown)}", fields=sorted(unknown))

    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValidationError("账户名不能为空", field="name")
    fields["name"] = name
    fields.setdefault("currency", DEFAULT_CURRENCY)
    _validate_account_fields(fields)
    _assert_name_available(session, name)

    account = Account(**fields)
    session.add(account)
    session.flush()
    audit.record(
        session,
        entity="account",
        entity_id=account.id,
        action="create",
        changes={"name": {"to": account.name}, "type": {"to": account.type}},
    )
    return account


def update_account(session: Session, account_id: int, **changes: Any) -> Account:
    """更新账户；只允许白名单字段，并记录字段级差异。"""
    unknown = set(changes) - ACCOUNT_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的账户字段：{sorted(unknown)}", fields=sorted(unknown))

    account = get_account(session, account_id)
    if "name" in changes:
        new_name = str(changes["name"] or "").strip()
        if not new_name:
            raise ValidationError("账户名不能为空", field="name")
        changes["name"] = new_name
        _assert_name_available(session, new_name, exclude_id=account_id)

    _validate_account_fields(changes)

    before = audit.snapshot(account, changes.keys())
    for key, value in changes.items():
        setattr(account, key, value)
    session.flush()

    audit.record_diff(
        session,
        entity="account",
        entity_id=account.id,
        action="update",
        before=before,
        after={key: getattr(account, key) for key in changes},
    )
    return account


def delete_account(session: Session, account_id: int) -> None:
    """软删除账户。

    **有未删除流水时拒绝删除** —— 直接删会让那些流水找不到账户，
    统计口径随之崩塌。这比"删除成功但报表少了几笔"要诚实得多。
    界面应引导用户先迁移流水或归档账户。
    """
    account = get_account(session, account_id)
    used = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.deleted_at.is_(None),
            (Transaction.account_id == account_id) | (Transaction.to_account_id == account_id),
        )
    )
    if used:
        raise ConflictError(
            f"该账户下还有 {used} 笔流水，请先迁移或删除这些流水，或将账户归档",
            account_id=account_id,
            transaction_count=int(used or 0),
        )

    account.soft_delete()
    session.flush()
    audit.record(session, entity="account", entity_id=account_id, action="delete")


def restore_account(session: Session, account_id: int) -> Account:
    """从回收站恢复账户。"""
    account = get_account(session, account_id, include_deleted=True)
    if not account.is_deleted:
        return account
    _assert_name_available(session, account.name, exclude_id=account.id)
    account.restore()
    session.flush()
    audit.record(session, entity="account", entity_id=account_id, action="restore")
    return account


def _validate_account_fields(fields: dict[str, Any]) -> None:
    """账户字段的业务校验（数据库约束之外的部分）。"""
    if "type" in fields and fields["type"] not in {member.value for member in AccountType}:
        raise ValidationError(f"未知账户类型：{fields['type']}", field="type")
    for day_field in ("bill_day", "due_day"):
        value = fields.get(day_field)
        if value is not None and not (1 <= int(value) <= 28):
            # 限制到 28 是为了让"每月 X 日"在 2 月也成立 —— 否则要引入
            # "当月最后一天"的规则，复杂度陡增而收益很小
            raise ValidationError(f"{day_field} 必须在 1–28 之间", field=day_field, value=value)
    for money_field in ("initial_balance_minor", "credit_limit_minor"):
        value = fields.get(money_field)
        if value is not None and not isinstance(value, int):
            raise ValidationError(f"{money_field} 必须是整数最小单位", field=money_field)
    if fields.get("credit_limit_minor") is not None and int(fields["credit_limit_minor"]) < 0:
        raise ValidationError("信用额度不能为负", field="credit_limit_minor")


# -----------------------------------------------------------------------------
# 余额
# -----------------------------------------------------------------------------
def _balance_expression():  # type: ignore[no-untyped-def]
    """构造"单账户净影响"的 SQL 表达式（见模块 docstring 的公式）。

    返回类型是 SQLAlchemy 的表达式对象，标注具体类型收益不大，
    反而会在每次升级 SQLAlchemy 时被迫跟着改。
    """
    signed = func.sum(
        case(
            (Transaction.direction == "in", Transaction.amount_minor),
            else_=-Transaction.amount_minor,
        )
    )
    return signed


def balances_by_account(session: Session, *, include_archived: bool = True) -> dict[int, int]:
    """一次性算出所有账户的余额，避免按账户循环查询（N+1）。

    返回 ``{account_id: balance_minor}``；没有流水的账户也会出现（值为起点余额）。
    """
    accounts = list_accounts(session, include_archived=include_archived)
    balances: dict[int, int] = {account.id: account.initial_balance_minor for account in accounts}

    outgoing = (
        select(
            Transaction.account_id.label("account_id"),
            _balance_expression().label("net"),
        )
        .where(Transaction.deleted_at.is_(None), Transaction.status != "void")
        .group_by(Transaction.account_id)
    )
    for account_id, net in session.execute(outgoing).all():
        if account_id in balances:
            balances[account_id] += int(net or 0)

    # 转入：只统计真正带目标账户的流水（转账），金额无条件为正向
    incoming = (
        select(
            Transaction.to_account_id.label("account_id"),
            func.sum(Transaction.amount_minor).label("net"),
        )
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.status != "void",
            Transaction.to_account_id.is_not(None),
        )
        .group_by(Transaction.to_account_id)
    )
    for account_id, net in session.execute(incoming).all():
        if account_id in balances:
            balances[account_id] += int(net or 0)

    return balances


def account_balance(session: Session, account_id: int) -> int:
    """单个账户的余额。"""
    get_account(session, account_id, include_deleted=True)
    return balances_by_account(session).get(account_id, 0)


def overview(session: Session, *, include_archived: bool = False) -> dict[str, Any]:
    """资产总览：账户列表 + 资产/负债/净值汇总。

    **资产与负债按余额正负分类**，而不是按账户类型：
    信用卡多还款会变成正余额（相当于银行存款），按类型硬分类会把这类情况算错。
    类型只用于界面分组与图标选择。
    """
    accounts = list_accounts(session, include_archived=include_archived)
    balances = balances_by_account(session)

    items: list[dict[str, Any]] = []
    total_positive = 0
    total_negative = 0
    counted = 0
    for account in accounts:
        balance = balances.get(account.id, account.initial_balance_minor)
        if account.include_in_net_worth:
            counted += 1
            if balance >= 0:
                total_positive += balance
            else:
                total_negative += balance
        items.append(
            {
                "id": account.id,
                "name": account.name,
                "type": account.type,
                "currency": account.currency,
                "icon": account.icon,
                "color": account.color,
                "institution": account.institution,
                "card_no_tail": account.card_no_tail,
                "balance_minor": balance,
                "initial_balance_minor": account.initial_balance_minor,
                "credit_limit_minor": account.credit_limit_minor,
                "include_in_net_worth": account.include_in_net_worth,
                "is_archived": account.is_archived,
                "is_liability_type": account.type in LIABILITY_TYPES,
                "sort_order": account.sort_order,
            }
        )

    return {
        "accounts": items,
        "assets_minor": total_positive,
        "liabilities_minor": -total_negative,
        "net_worth_minor": total_positive + total_negative,
        "account_count": len(items),
        "counted_in_net_worth": counted,
    }
