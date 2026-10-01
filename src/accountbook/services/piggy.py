"""存钱罐与储蓄目标（P4 / 需求 16）。

三条贯穿全篇的设计原则
======================

**一、余额永远算出来，不存下来。**
`balance = SUM(amount_minor)`。存一个余额列意味着每笔存入都要记得更新它，
而总有一次会忘了（导入、插件、手工改库），然后就再也对不上账 ——
流水那边已经吃过这个教训，这里不重复。

**二、归集规则分成"事件驱动"与"时间驱动"两族，不装作它们一样。**
* 事件驱动（四舍五入 / 收入百分比 / 分类触发）挂在**某笔流水**上：
  "这笔账记完之后顺手归集"。
* 时间驱动（每日定额 / 每周定额 / 月度结余）挂在**某一天**上：
  "应用今天启动了，该攒的攒上"。

曾经想写成一个统一的 `apply(rule, context)`，但那样每族都要在函数里
判断"我拿到的 context 里有没有我要的东西"，而传错了参数（比如给定额规则
传了一笔流水）会静默什么都不做 —— 静默比报错难查得多。分成两个函数后，
类型签名本身就说明了"这条规则需要什么"。

**三、幂等靠 `last_run_date` 与 `transaction_id`。**
应用可能一天被开关很多次；事件驱动规则可能被重复触达（编辑流水后重算）。
"多攒了一笔"用户很难发现 —— 罐子里多几块钱不会引起注意 ——
因此幂等必须由数据结构保证，而不是靠调用方自觉。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.domain import TransactionSource, TransactionType
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.money import DEFAULT_CURRENCY
from ..core.periods import period_key
from ..db.models import (
    Account,
    Goal,
    GoalContribution,
    PiggyBank,
    PiggyBankDeposit,
    PiggyBankRule,
    Transaction,
)

__all__ = [
    "BANK_KINDS",
    "BANK_STATUSES",
    "DEPOSIT_KINDS",
    "ETA_HALF_LIFE_DAYS",
    "ETA_LOOKBACK_DAYS",
    "GOAL_KINDS",
    "MILESTONES",
    "RULE_STRATEGIES",
    "achieve_bank",
    "add_deposit",
    "add_goal",
    "apply_rules_to_transactions",
    "balance",
    "bank_detail",
    "contribute",
    "create_bank",
    "delete_bank",
    "delete_contribution",
    "delete_deposit",
    "delete_goal",
    "delete_rule",
    "due_rules",
    "estimate_completion",
    "get_bank",
    "get_goal",
    "get_rule",
    "goal_saved",
    "inject_bank_into_goal",
    "list_banks",
    "list_contributions",
    "list_deposits",
    "list_goals",
    "milestones_reached",
    "progress",
    "restore_bank",
    "restore_goal",
    "run_due_rules",
    "serialize_bank",
    "serialize_deposit",
    "serialize_goal",
    "serialize_rule",
    "update_bank",
    "update_goal",
    "upsert_rule",
]

_logger = logging.getLogger(__name__)

BANK_KINDS = ("one_time", "long_term", "shared")
BANK_STATUSES = ("active", "achieved", "paused", "abandoned")
GOAL_KINDS = ("purchase", "emergency", "travel", "education", "other")
RULE_STRATEGIES = (
    "roundup",
    "daily_fixed",
    "weekly_fixed",
    "income_percent",
    "monthly_surplus",
    "category_trigger",
)
DEPOSIT_KINDS = ("manual", "auto", "roundup", "change", "milestone", "withdraw", "settle")

#: 里程碑。达成时界面放礼花，且只放一次（`celebrated` 标记）
MILESTONES = (25, 50, 75, 100)

#: 预计达成日的观察窗口与半衰期（天）。
#: 半衰期 15 天意味着"两周前的存入只算今天的一半" ——
#: 这个尺度下"最近一个月开始偷懒"会明显拉低估算，而"上周多存了一笔"
#: 不会把估算拉飞。
ETA_LOOKBACK_DAYS = 90
ETA_HALF_LIFE_DAYS = 15

#: 时间驱动策略的定义：(需要间隔天数, 是否是"每月一次")
_TIME_DRIVEN = {
    "daily_fixed": 1,
    "weekly_fixed": 7,
    "monthly_surplus": 0,  # 0 表示按自然月判断，而不是按天数间隔
}


# -----------------------------------------------------------------------------
# 内部工具
# -----------------------------------------------------------------------------
def _require_bank(session: Session, bank_id: int, *, include_deleted: bool = False) -> PiggyBank:
    row = session.get(PiggyBank, bank_id)
    if row is None or (row.deleted_at is not None and not include_deleted):
        raise NotFoundError("存钱罐不存在", entity="piggy_bank", entity_id=bank_id)
    return row


def _require_goal(session: Session, goal_id: int, *, include_deleted: bool = False) -> Goal:
    row = session.get(Goal, goal_id)
    if row is None or (row.deleted_at is not None and not include_deleted):
        raise NotFoundError("储蓄目标不存在", entity="goal", entity_id=goal_id)
    return row


def _load_category_ids(rule: PiggyBankRule) -> list[int]:
    """解析分类 id 列表。坏 JSON 当作空列表，而不是让整个页面打不开。"""
    try:
        parsed = json.loads(rule.category_ids or "[]")
    except (TypeError, ValueError):
        _logger.warning("归集规则 #%s 的分类列表不是合法 JSON，按空处理", rule.id)
        return []
    if not isinstance(parsed, list):
        return []
    return [int(item) for item in parsed if isinstance(item, (int, str)) and str(item).isdigit()]


def _local_today() -> date:
    return datetime.now().date()


def _parse_occurred(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now().replace(microsecond=0)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None, microsecond=0)
    except ValueError as error:
        raise ValidationError(f"无法解析时间：{value}", field="occurred_at") from error


# -----------------------------------------------------------------------------
# 存钱罐 CRUD
# -----------------------------------------------------------------------------
def create_bank(
    session: Session,
    *,
    name: str,
    target_amount_minor: int,
    target_name: str = "",
    currency: str = DEFAULT_CURRENCY,
    deadline: date | str | None = None,
    kind: str = "one_time",
    member_id: int | None = None,
    priority: int = 5,
    skin: str = "classic",
    hide_amount: bool = False,
    goal_id: int | None = None,
    note: str = "",
    initial_minor: int = 0,
) -> PiggyBank:
    """建一个存钱罐。

    ``initial_minor`` 允许建罐时就放一笔进去（"这个罐子里已经有 200 了"），
    否则用户要先建罐、再手动存一次，多一步且看起来像没生效。
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("存钱罐名称不能为空", field="name")
    if target_amount_minor <= 0:
        raise ValidationError("目标金额必须大于 0", field="target_amount_minor")
    if kind not in BANK_KINDS:
        raise ValidationError(f"未知的罐子类型：{kind}", field="kind", allowed=list(BANK_KINDS))
    if not 0 <= priority <= 9:
        raise ValidationError("优先级必须在 0–9 之间", field="priority")
    if goal_id is not None:
        _require_goal(session, goal_id)

    parsed_deadline: date | None = None
    if deadline is not None:
        parsed_deadline = (
            deadline
            if isinstance(deadline, date) and not isinstance(deadline, datetime)
            else date.fromisoformat(str(deadline))
        )

    bank = PiggyBank(
        name=cleaned,
        target_name=(target_name or "").strip(),
        target_amount_minor=int(target_amount_minor),
        currency=currency,
        deadline=parsed_deadline,
        kind=kind,
        status="active",
        member_id=member_id,
        priority=int(priority),
        skin=skin,
        hide_amount=bool(hide_amount),
        goal_id=goal_id,
        note=(note or "").strip(),
    )
    session.add(bank)
    session.flush()

    if initial_minor:
        add_deposit(
            session,
            bank.id,
            amount_minor=int(initial_minor),
            kind="manual",
            note="建罐初始金额",
        )
    return bank


def update_bank(session: Session, bank_id: int, **changes: Any) -> PiggyBank:
    bank = _require_bank(session, bank_id)
    mutable = {
        "name",
        "target_name",
        "target_amount_minor",
        "currency",
        "deadline",
        "kind",
        "status",
        "member_id",
        "priority",
        "skin",
        "hide_amount",
        "goal_id",
        "note",
        "sort_order",
        "celebrated",
    }
    for key, value in changes.items():
        if key not in mutable:
            continue
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise ValidationError("存钱罐名称不能为空", field="name")
        if key == "target_amount_minor" and int(value) <= 0:
            raise ValidationError("目标金额必须大于 0", field="target_amount_minor")
        if key == "kind" and value not in BANK_KINDS:
            raise ValidationError(f"未知的罐子类型：{value}", field="kind")
        if key == "status" and value not in BANK_STATUSES:
            raise ValidationError(f"未知的状态：{value}", field="status")
        if key == "priority" and not 0 <= int(value) <= 9:
            raise ValidationError("优先级必须在 0–9 之间", field="priority")
        if key == "deadline" and isinstance(value, str) and value:
            value = date.fromisoformat(value)
        if key == "goal_id" and value is not None:
            _require_goal(session, int(value))
        setattr(bank, key, value)
    session.flush()
    return bank


def list_banks(
    session: Session, *, status: str | None = None, include_deleted: bool = False
) -> list[PiggyBank]:
    statement = select(PiggyBank)
    if not include_deleted:
        statement = statement.where(PiggyBank.deleted_at.is_(None))
    if status:
        statement = statement.where(PiggyBank.status == status)
    # 优先级高的在前，其次按 sort_order，最后按 id 保证顺序稳定
    statement = statement.order_by(PiggyBank.priority.desc(), PiggyBank.sort_order, PiggyBank.id)
    return list(session.scalars(statement).all())


def get_bank(session: Session, bank_id: int) -> PiggyBank:
    return _require_bank(session, bank_id)


def delete_bank(session: Session, bank_id: int) -> None:
    bank = _require_bank(session, bank_id)
    bank.deleted_at = datetime.now()
    # 必须 flush：会话关了 autoflush，不 flush 的话紧接着的 SELECT
    # 仍然会看到这条"已删除"的记录，表现为"删了但还在列表里"
    session.flush()


def restore_bank(session: Session, bank_id: int) -> PiggyBank:
    bank = _require_bank(session, bank_id, include_deleted=True)
    bank.deleted_at = None
    # 恢复后重新算一次达成状态：罐子可能在被删期间"其实已经攒够了"
    _refresh_achievement(session, bank)
    session.flush()
    return bank


# -----------------------------------------------------------------------------
# 存入 / 取出
# -----------------------------------------------------------------------------
def add_deposit(
    session: Session,
    bank_id: int,
    *,
    amount_minor: int,
    kind: str = "manual",
    occurred_at: datetime | str | None = None,
    tz_offset_minutes: int = 0,
    source_account_id: int | None = None,
    transaction_id: int | None = None,
    note: str = "",
) -> PiggyBankDeposit:
    """往罐子里放钱（负数为取出）。

    取出时**允许把罐子取空但不容许取成负数**：罐子里有多少才能拿多少 ——
    负数余额在这里没有任何合理解释，而它一旦出现，
    "液面填充"的动画与百分比都会变成无意义的数。
    """
    bank = _require_bank(session, bank_id)
    if amount_minor == 0:
        raise ValidationError("金额不能为 0", field="amount_minor")
    if kind not in DEPOSIT_KINDS:
        raise ValidationError(f"未知的存入类型：{kind}", field="kind", allowed=list(DEPOSIT_KINDS))
    if source_account_id is not None and session.get(Account, source_account_id) is None:
        raise NotFoundError("账户不存在", entity="account", entity_id=source_account_id)

    if amount_minor < 0:
        current = balance(session, bank_id)
        if current + amount_minor < 0:
            raise ConflictError(
                "罐子里的钱不够取出这么多",
                entity="piggy_bank",
                entity_id=bank_id,
                balance_minor=current,
                requested_minor=-amount_minor,
                suggestion="先确认罐子余额，或改为部分取出",
            )

    deposit = PiggyBankDeposit(
        piggy_bank_id=bank_id,
        amount_minor=int(amount_minor),
        occurred_at=_parse_occurred(occurred_at),
        tz_offset_minutes=int(tz_offset_minutes),
        source_account_id=source_account_id,
        transaction_id=transaction_id,
        kind=kind,
        note=(note or "").strip(),
    )
    session.add(deposit)
    session.flush()
    _refresh_achievement(session, bank)
    return deposit


def delete_deposit(session: Session, deposit_id: int) -> None:
    deposit = session.get(PiggyBankDeposit, deposit_id)
    if deposit is None:
        raise NotFoundError("存入记录不存在", entity="piggy_bank_deposit", entity_id=deposit_id)
    # 删掉一笔**存入**会让余额变小，删掉一笔取出会让余额变大。
    # 因此不能只判断金额的正负，而要判断"删掉之后余额还剩多少" ——
    # 最初我按 `amount_minor < 0` 来拦，方向正好反了。
    current = balance(session, deposit.piggy_bank_id)
    remaining = current - deposit.amount_minor
    if remaining < 0:
        raise ConflictError(
            "删除这笔记录会让罐子余额变成负数",
            entity="piggy_bank_deposit",
            entity_id=deposit_id,
            balance_minor=current,
            remaining_minor=remaining,
            suggestion="先删掉相关的取出记录，或改为部分调整",
        )
    bank_id = deposit.piggy_bank_id
    session.delete(deposit)
    session.flush()
    _refresh_achievement(session, _require_bank(session, bank_id))


def list_deposits(session: Session, bank_id: int, *, limit: int = 200) -> list[PiggyBankDeposit]:
    return list(
        session.scalars(
            select(PiggyBankDeposit)
            .where(PiggyBankDeposit.piggy_bank_id == bank_id)
            .order_by(PiggyBankDeposit.occurred_at.desc(), PiggyBankDeposit.id.desc())
            .limit(limit)
        ).all()
    )


def balance(session: Session, bank_id: int) -> int:
    """罐子余额 = 所有存入之和。**永远算出来**（见模块 docstring）。"""
    total = session.scalar(
        select(func.coalesce(func.sum(PiggyBankDeposit.amount_minor), 0)).where(
            PiggyBankDeposit.piggy_bank_id == bank_id
        )
    )
    return int(total or 0)


def _sum_all_balances(session: Session, bank_ids: Iterable[int]) -> dict[int, int]:
    """一次查出多个罐子的余额。

    列表页要为每个罐子画液面，逐个查会变成 N 次查询 ——
    10 个罐子就是 10 次往返，而它们的形状完全一样。
    """
    ids = list(bank_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(
            PiggyBankDeposit.piggy_bank_id,
            func.coalesce(func.sum(PiggyBankDeposit.amount_minor), 0),
        )
        .where(PiggyBankDeposit.piggy_bank_id.in_(ids))
        .group_by(PiggyBankDeposit.piggy_bank_id)
    ).all()
    result = {int(row[0]): int(row[1]) for row in rows}
    for bank_id in ids:
        result.setdefault(bank_id, 0)
    return result


# -----------------------------------------------------------------------------
# 进度 / 里程碑 / 达成
# -----------------------------------------------------------------------------
def progress(balance_minor: int, target_minor: int) -> float:
    """完成比例。**不夹到 1.0**（超出目标要能看出来），但目标为 0 时返回 0。"""
    if target_minor <= 0:
        return 0.0
    return balance_minor / target_minor


def milestones_reached(balance_minor: int, target_minor: int) -> list[int]:
    ratio = progress(balance_minor, target_minor)
    return [mark for mark in MILESTONES if ratio >= mark / 100]


def _refresh_achievement(session: Session, bank: PiggyBank) -> None:
    """根据余额刷新达成状态。

    只在**跨过**目标时改状态：已经达成的罐子又被取走一部分时，
    状态**保持"已达成"** —— 用户看到"我攒到过"是有意义的，
    而把它变回"进行中"会让庆祝与历史记录都失去意义。
    想重新攒可以新建一个罐子。
    """
    if bank.status in {"abandoned", "paused", "achieved"}:
        return
    current = balance(session, bank.id)
    if current >= bank.target_amount_minor:
        bank.status = "achieved"
        bank.achieved_at = bank.achieved_at or datetime.now()


def achieve_bank(
    session: Session,
    bank_id: int,
    *,
    settle: bool = True,
    account_id: int | None = None,
    occurred_at: datetime | str | None = None,
    create_transaction: bool = True,
) -> dict[str, Any]:
    """达成后一键生成"购买支出"流水并结清罐子。

    设计要点
    --------
    * **结清用一笔负数的 `settle` 记录，而不是删掉存入历史。**
      罐子的历史（什么时候攒了多少）本身就是这个功能的产物，
      清空它等于把这个罐子变成从没存在过。
    * 生成的流水金额 = **罐子余额**而不是目标金额。罐子里可能比目标更多
      （攒超了），而"账实相符"优先于"数字好看"。
    * 可以只结清不生成流水（`create_transaction=False`）：钱可能早就花掉了，
      只是忘了在罐子上记账。
    """
    bank = _require_bank(session, bank_id)
    current = balance(session, bank_id)
    if current <= 0:
        raise ConflictError(
            "罐子是空的，没有可结清的金额",
            entity="piggy_bank",
            entity_id=bank_id,
            balance_minor=current,
        )
    if create_transaction and account_id is None:
        raise ValidationError("生成支出流水必须指定账户", field="account_id")

    transaction_id: int | None = None
    if create_transaction:
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)
        transaction = Transaction(
            type=TransactionType.EXPENSE.value,
            account_id=account_id,
            amount_minor=current,
            occurred_at=_parse_occurred(occurred_at),
            payee=(bank.target_name or bank.name)[:80],
            note=f"存钱罐「{bank.name}」达成后购买",
            source=TransactionSource.MANUAL.value,
            status="cleared",
        )
        session.add(transaction)
        session.flush()
        transaction_id = transaction.id

    if settle:
        session.add(
            PiggyBankDeposit(
                piggy_bank_id=bank_id,
                amount_minor=-current,
                occurred_at=_parse_occurred(occurred_at),
                kind="settle",
                transaction_id=transaction_id,
                note="达成后结清",
            )
        )

    bank.status = "achieved"
    bank.achieved_at = bank.achieved_at or datetime.now()
    session.flush()
    return {
        "bank": serialize_bank(session, bank),
        "settled_minor": current if settle else 0,
        "transaction_id": transaction_id,
        "remaining_minor": balance(session, bank_id),
    }


# -----------------------------------------------------------------------------
# 预计达成日：线性 + 加权双口径
# -----------------------------------------------------------------------------
def _daily_amounts(deposits: list[PiggyBankDeposit], today: date) -> dict[date, int]:
    """按本地日期汇总每日净存入。同一天多笔合并 —— 逐笔加权
    会让"一天存 10 次零钱"的人被算成速度极快。"""
    buckets: dict[date, int] = {}
    for item in deposits:
        day = item.occurred_at.date()
        if day > today:
            continue
        buckets[day] = buckets.get(day, 0) + item.amount_minor
    return buckets


def estimate_completion(
    session: Session,
    bank: PiggyBank,
    *,
    today: date | None = None,
    balance_minor: int | None = None,
) -> dict[str, Any]:
    """预计达成日。**同时给出线性与加权两个口径**。

    为什么给两个而不是挑一个
    ------------------------
    线性（累计 ÷ 已过天数）稳定、可解释，但它对"最近停止存钱了"**完全无感** ——
    攒了三个月、最近一个月一分没存，线性估算仍然乐观。

    加权（近 90 天按 15 天半衰期的指数加权日均）对节奏变化敏感，
    但它也会被"上周一次性存了一大笔"拉飞。

    两个口径各有各的盲区，而**挑一个藏起另一个等于替用户做了他没授权的假设**。
    因此两个都返回，界面上并排显示，并在明显分歧时提示"两个口径差距较大" ——
    那个分歧本身就是有用的信息（说明攒钱节奏在变）。
    """
    today = today or _local_today()
    current = balance(session, bank.id) if balance_minor is None else balance_minor
    remaining = bank.target_amount_minor - current

    deposits = list(
        session.scalars(select(PiggyBankDeposit).where(PiggyBankDeposit.piggy_bank_id == bank.id)).all()
    )
    buckets = _daily_amounts(deposits, today)

    result: dict[str, Any] = {
        "balance_minor": current,
        "remaining_minor": max(0, remaining),
        "ratio": progress(current, bank.target_amount_minor),
        "deadline": bank.deadline.isoformat() if bank.deadline else None,
        "linear": None,
        "weighted": None,
        "divergent": False,
        "days_to_deadline": (bank.deadline - today).days if bank.deadline else None,
    }
    if remaining <= 0:
        # 已达成：无需估算，也不该给出"还要 0 天"这种看起来像在催的数字
        result["achieved"] = True
        return result
    result["achieved"] = False

    # ---- 线性：累计 ÷ 已过天数 ----
    if buckets:
        first_day = min(buckets)
        elapsed = max(1, (today - first_day).days + 1)
        linear_balance = sum(buckets.values())
        linear_rate = linear_balance / elapsed
    else:
        elapsed = 0
        linear_rate = 0.0
    result["elapsed_days"] = elapsed

    def _finish(rate: float) -> dict[str, Any] | None:
        if rate <= 0:
            return None
        days = math.ceil(remaining / rate)
        return {
            "rate_per_day_minor": round(rate, 2),
            "days": days,
            "eta": (today + timedelta(days=days)).isoformat(),
        }

    result["linear"] = _finish(linear_rate)

    # ---- 加权：近 90 天逐日净存入的指数加权平均 ----
    window_start = today - timedelta(days=ETA_LOOKBACK_DAYS - 1)
    weighted_sum = 0.0
    weight_total = 0.0
    for offset in range(ETA_LOOKBACK_DAYS):
        day = window_start + timedelta(days=offset)
        weight = 0.5 ** ((today - day).days / ETA_HALF_LIFE_DAYS)
        weighted_sum += buckets.get(day, 0) * weight
        weight_total += weight
    weighted_rate = weighted_sum / weight_total if weight_total > 0 else 0.0
    result["weighted"] = _finish(weighted_rate)

    # 两个口径给出不同的"天数"且差距超过一倍时，值得提示
    if result["linear"] and result["weighted"]:
        a = result["linear"]["days"]
        b = result["weighted"]["days"]
        result["divergent"] = max(a, b) > 2 * max(1, min(a, b))

    # ---- 与期限的差距 ----
    if bank.deadline is not None:
        days_left = (bank.deadline - today).days
        result["on_track"] = bool(result["weighted"] and result["weighted"]["days"] <= days_left)
        result["required_per_day_minor"] = round(remaining / days_left, 2) if days_left > 0 else None
    else:
        result["on_track"] = None
        result["required_per_day_minor"] = None
    return result


# -----------------------------------------------------------------------------
# 归集规则
# -----------------------------------------------------------------------------
def get_rule(session: Session, bank_id: int) -> PiggyBankRule | None:
    return session.scalar(select(PiggyBankRule).where(PiggyBankRule.piggy_bank_id == bank_id))


def upsert_rule(
    session: Session,
    bank_id: int,
    *,
    strategy: str,
    enabled: bool = True,
    roundup_unit_minor: int = 100,
    fixed_amount_minor: int = 0,
    percent_bps: int = 0,
    category_ids: list[int] | None = None,
    account_id: int | None = None,
    deduct_from_account: bool = False,
) -> PiggyBankRule:
    _require_bank(session, bank_id)
    if strategy not in RULE_STRATEGIES:
        raise ValidationError(f"未知的归集策略：{strategy}", field="strategy", allowed=list(RULE_STRATEGIES))
    if roundup_unit_minor <= 0:
        raise ValidationError("四舍五入单位必须大于 0", field="roundup_unit_minor")
    if fixed_amount_minor < 0:
        raise ValidationError("定额不能为负", field="fixed_amount_minor")
    if not 0 <= percent_bps <= 10000:
        raise ValidationError("百分比必须在 0–100% 之间", field="percent_bps")
    if strategy in {"daily_fixed", "weekly_fixed"} and fixed_amount_minor <= 0:
        raise ValidationError("定额策略必须给出大于 0 的金额", field="fixed_amount_minor")
    # "从账户扣减"必须知道从哪个账户扣 —— 否则归集成功但钱没少，
    # 用户会对不上账，而且很难想到是配置缺了一项
    if deduct_from_account and account_id is None:
        raise ValidationError("选择从账户扣减时必须指定账户", field="account_id")
    if account_id is not None and session.get(Account, account_id) is None:
        raise NotFoundError("账户不存在", entity="account", entity_id=account_id)

    rule = get_rule(session, bank_id)
    payload = {
        "strategy": strategy,
        "enabled": bool(enabled),
        "roundup_unit_minor": int(roundup_unit_minor),
        "fixed_amount_minor": int(fixed_amount_minor),
        "percent_bps": int(percent_bps),
        "category_ids": json.dumps(sorted(set(category_ids or []))),
        "account_id": account_id,
        "deduct_from_account": bool(deduct_from_account),
    }
    if rule is None:
        rule = PiggyBankRule(piggy_bank_id=bank_id, **payload)
        session.add(rule)
    else:
        for key, value in payload.items():
            setattr(rule, key, value)
    session.flush()
    return rule


def delete_rule(session: Session, bank_id: int) -> None:
    rule = get_rule(session, bank_id)
    if rule is None:
        raise NotFoundError("归集规则不存在", entity="piggy_bank_rule", bank_id=bank_id)
    session.delete(rule)
    session.flush()


def _roundup_amount(amount_minor: int, unit: int) -> int:
    """凑整到下一个 ``unit`` 的倍数。

    正好是整倍数时返回 **0**（没有零钱可攒）—— 返回 ``unit`` 会凭空多存一笔，
    而用户看到"买 100 元的东西，罐子里多了 1 元"会认为计算错了。
    只对**支出**做四舍五入：收入凑整没有语义。
    """
    if amount_minor <= 0:
        return 0
    remainder = amount_minor % unit
    return 0 if remainder == 0 else unit - remainder


def _time_driven_due(rule: PiggyBankRule, today: date) -> bool:
    interval = _TIME_DRIVEN[rule.strategy]
    if rule.last_run_date is None:
        return True
    if interval == 1:
        return rule.last_run_date < today
    if interval == 7:
        return (today - rule.last_run_date).days >= 7
    # 自然月：同一个"年-月"里只跑一次
    return period_key("monthly", rule.last_run_date) != period_key("monthly", today)


def due_rules(session: Session, *, today: date | None = None) -> list[dict[str, Any]]:
    """哪些时间驱动规则该跑了。只看不写，便于界面预览。"""
    today = today or _local_today()
    result: list[dict[str, Any]] = []
    for rule in session.scalars(select(PiggyBankRule).where(PiggyBankRule.enabled.is_(True))).all():
        if rule.strategy not in _TIME_DRIVEN:
            continue
        bank = session.get(PiggyBank, rule.piggy_bank_id)
        if bank is None or bank.deleted_at is not None or bank.status != "active":
            continue
        if _time_driven_due(rule, today):
            result.append({"bank_id": bank.id, "bank_name": bank.name, "strategy": rule.strategy})
    return result


def _month_surplus(session: Session, day: date) -> int:
    """当月结余 = 当月收入 − 当月支出。转账不计入（与全局口径一致）。"""
    start = day.replace(day=1)
    rows = session.execute(
        select(Transaction.type, func.coalesce(func.sum(Transaction.amount_minor), 0))
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.occurred_at >= datetime.combine(start, datetime.min.time()),
            Transaction.occurred_at <= datetime.combine(day, datetime.max.time()),
            Transaction.type.in_([TransactionType.INCOME.value, TransactionType.EXPENSE.value]),
        )
        .group_by(Transaction.type)
    ).all()
    totals = {str(row[0]): int(row[1]) for row in rows}
    return totals.get(TransactionType.INCOME.value, 0) - totals.get(TransactionType.EXPENSE.value, 0)


def _rule_amount_for_date(session: Session, rule: PiggyBankRule, today: date) -> int:
    if rule.strategy == "daily_fixed":
        return rule.fixed_amount_minor
    if rule.strategy == "weekly_fixed":
        return rule.fixed_amount_minor
    if rule.strategy == "monthly_surplus":
        surplus = _month_surplus(session, today)
        if surplus <= 0:
            return 0
        share = rule.percent_bps or 10000
        return surplus * share // 10000
    return 0


def _rule_amount_for_transaction(session: Session, rule: PiggyBankRule, transaction: Transaction) -> int:
    if rule.strategy == "roundup":
        # 只对支出凑整：收入凑整没有语义
        if transaction.type != TransactionType.EXPENSE.value:
            return 0
        return _roundup_amount(transaction.amount_minor, rule.roundup_unit_minor)
    if rule.strategy == "income_percent":
        if transaction.type != TransactionType.INCOME.value:
            return 0
        return transaction.amount_minor * rule.percent_bps // 10000
    if rule.strategy == "category_trigger":
        if transaction.type != TransactionType.EXPENSE.value:
            return 0
        allowed = _load_category_ids(rule)
        if not allowed or transaction.category_id not in allowed:
            return 0
        if rule.percent_bps:
            return transaction.amount_minor * rule.percent_bps // 10000
        return rule.fixed_amount_minor
    return 0


def _kind_for_strategy(strategy: str) -> str:
    return {
        "roundup": "roundup",
        "monthly_surplus": "auto",
        "category_trigger": "auto",
    }.get(strategy, "auto")


def run_due_rules(
    session: Session, *, today: date | None = None, dry_run: bool = False
) -> list[dict[str, Any]]:
    """执行到期的时间驱动规则。

    ``dry_run`` 返回"会存多少"而不落库 —— 界面需要能预览，
    而"先执行再看结果"对自动扣钱的功能是不可接受的。
    """
    today = today or _local_today()
    applied: list[dict[str, Any]] = []
    for item in due_rules(session, today=today):
        rule = get_rule(session, item["bank_id"])
        if rule is None:
            continue
        amount = _rule_amount_for_date(session, rule, today)
        applied.append({**item, "amount_minor": amount, "dry_run": dry_run})
        if dry_run or amount <= 0:
            continue
        add_deposit(
            session,
            rule.piggy_bank_id,
            amount_minor=amount,
            kind=_kind_for_strategy(rule.strategy),
            occurred_at=datetime.combine(today, datetime.min.time()).replace(hour=12),
            source_account_id=rule.account_id,
            note=f"自动归集（{rule.strategy}）",
        )
        rule.last_run_date = today
    session.flush()
    return applied


def apply_rules_to_transactions(
    session: Session, transaction_ids: Iterable[int], *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """对给定的流水应用事件驱动规则（四舍五入 / 收入百分比 / 分类触发）。

    幂等：同一笔流水 + 同一个罐子已经产生过存款时直接跳过。
    编辑一笔流水后重算会再次走到这里，而"改个备注就多攒一次钱"
    是那种用户几乎不可能发现、却会持续虚增罐子的 bug。
    """
    ids = list(transaction_ids)
    if not ids:
        return []
    transactions = {
        row.id: row
        for row in session.scalars(
            select(Transaction).where(Transaction.id.in_(ids), Transaction.deleted_at.is_(None))
        ).all()
    }
    results: list[dict[str, Any]] = []
    rules = session.scalars(select(PiggyBankRule).where(PiggyBankRule.enabled.is_(True))).all()
    for rule in rules:
        if rule.strategy not in {"roundup", "income_percent", "category_trigger"}:
            continue
        bank = session.get(PiggyBank, rule.piggy_bank_id)
        if bank is None or bank.deleted_at is not None or bank.status != "active":
            continue
        for transaction_id in ids:
            transaction = transactions.get(transaction_id)
            if transaction is None:
                continue
            already = session.scalar(
                select(PiggyBankDeposit.id).where(
                    PiggyBankDeposit.piggy_bank_id == bank.id,
                    PiggyBankDeposit.transaction_id == transaction_id,
                    PiggyBankDeposit.kind == _kind_for_strategy(rule.strategy),
                )
            )
            if already is not None:
                continue
            amount = _rule_amount_for_transaction(session, rule, transaction)
            if amount <= 0:
                continue
            results.append(
                {
                    "bank_id": bank.id,
                    "bank_name": bank.name,
                    "transaction_id": transaction_id,
                    "strategy": rule.strategy,
                    "amount_minor": amount,
                    "dry_run": dry_run,
                }
            )
            if dry_run:
                continue
            add_deposit(
                session,
                bank.id,
                amount_minor=amount,
                kind=_kind_for_strategy(rule.strategy),
                occurred_at=transaction.occurred_at,
                tz_offset_minutes=transaction.tz_offset_minutes,
                source_account_id=rule.account_id,
                transaction_id=transaction_id,
                note=f"自动归集（{rule.strategy}）",
            )
    session.flush()
    return results


# -----------------------------------------------------------------------------
# 储蓄目标
# -----------------------------------------------------------------------------
def add_goal(
    session: Session,
    *,
    name: str,
    target_amount_minor: int,
    currency: str = DEFAULT_CURRENCY,
    deadline: date | str | None = None,
    kind: str = "purchase",
    account_id: int | None = None,
    member_id: int | None = None,
    priority: int = 5,
    hide_amount: bool = False,
    note: str = "",
) -> Goal:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("目标名称不能为空", field="name")
    if target_amount_minor <= 0:
        raise ValidationError("目标金额必须大于 0", field="target_amount_minor")
    if kind not in GOAL_KINDS:
        raise ValidationError(f"未知的目标类型：{kind}", field="kind", allowed=list(GOAL_KINDS))
    if not 0 <= priority <= 9:
        raise ValidationError("优先级必须在 0–9 之间", field="priority")
    if account_id is not None:
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)

    parsed_deadline: date | None = None
    if deadline is not None:
        parsed_deadline = (
            deadline
            if isinstance(deadline, date) and not isinstance(deadline, datetime)
            else date.fromisoformat(str(deadline))
        )

    goal = Goal(
        name=cleaned,
        target_amount_minor=int(target_amount_minor),
        currency=currency,
        deadline=parsed_deadline,
        kind=kind,
        status="active",
        account_id=account_id,
        member_id=member_id,
        priority=int(priority),
        hide_amount=bool(hide_amount),
        note=(note or "").strip(),
    )
    session.add(goal)
    session.flush()
    return goal


def update_goal(session: Session, goal_id: int, **changes: Any) -> Goal:
    goal = _require_goal(session, goal_id)
    mutable = {
        "name",
        "target_amount_minor",
        "currency",
        "deadline",
        "kind",
        "status",
        "account_id",
        "member_id",
        "priority",
        "hide_amount",
        "note",
        "sort_order",
        "celebrated",
    }
    for key, value in changes.items():
        if key not in mutable:
            continue
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise ValidationError("目标名称不能为空", field="name")
        if key == "target_amount_minor" and int(value) <= 0:
            raise ValidationError("目标金额必须大于 0", field="target_amount_minor")
        if key == "kind" and value not in GOAL_KINDS:
            raise ValidationError(f"未知的目标类型：{value}", field="kind")
        if key == "account_id" and value is not None:
            account = session.get(Account, int(value))
            if account is None or account.deleted_at is not None:
                raise NotFoundError("账户不存在", entity="account", entity_id=value)
        if key == "deadline" and isinstance(value, str) and value:
            value = date.fromisoformat(value)
        setattr(goal, key, value)
    session.flush()
    return goal


def list_goals(session: Session, *, status: str | None = None, include_deleted: bool = False) -> list[Goal]:
    statement = select(Goal)
    if not include_deleted:
        statement = statement.where(Goal.deleted_at.is_(None))
    if status:
        statement = statement.where(Goal.status == status)
    statement = statement.order_by(Goal.priority.desc(), Goal.sort_order, Goal.id)
    return list(session.scalars(statement).all())


def get_goal(session: Session, goal_id: int) -> Goal:
    return _require_goal(session, goal_id)


def delete_goal(session: Session, goal_id: int) -> None:
    goal = _require_goal(session, goal_id)
    goal.deleted_at = datetime.now()
    session.flush()


def restore_goal(session: Session, goal_id: int) -> Goal:
    goal = _require_goal(session, goal_id, include_deleted=True)
    goal.deleted_at = None
    _refresh_goal_achievement(session, goal)
    session.flush()
    return goal


def contribute(
    session: Session,
    goal_id: int,
    *,
    amount_minor: int,
    occurred_at: datetime | str | None = None,
    tz_offset_minutes: int = 0,
    note: str = "",
) -> GoalContribution:
    """往目标里注入一笔钱（负数取出）。"""
    goal = _require_goal(session, goal_id)
    if amount_minor == 0:
        raise ValidationError("金额不能为 0", field="amount_minor")
    if amount_minor < 0:
        current = goal_saved(session, goal)
        if current + amount_minor < 0:
            raise ConflictError(
                "目标的已存金额不够取出这么多",
                entity="goal",
                entity_id=goal_id,
                saved_minor=current,
                requested_minor=-amount_minor,
            )
    row = GoalContribution(
        goal_id=goal_id,
        amount_minor=int(amount_minor),
        occurred_at=_parse_occurred(occurred_at),
        tz_offset_minutes=int(tz_offset_minutes),
        note=(note or "").strip(),
    )
    session.add(row)
    session.flush()
    _refresh_goal_achievement(session, goal)
    return row


def delete_contribution(session: Session, contribution_id: int) -> None:
    row = session.get(GoalContribution, contribution_id)
    if row is None:
        raise NotFoundError("注入记录不存在", entity="goal_contribution", entity_id=contribution_id)
    goal_id = row.goal_id
    session.delete(row)
    session.flush()
    _refresh_goal_achievement(session, _require_goal(session, goal_id))


def list_contributions(session: Session, goal_id: int, *, limit: int = 200) -> list[GoalContribution]:
    return list(
        session.scalars(
            select(GoalContribution)
            .where(GoalContribution.goal_id == goal_id)
            .order_by(GoalContribution.occurred_at.desc(), GoalContribution.id.desc())
            .limit(limit)
        ).all()
    )


def _account_balance(session: Session, account_id: int) -> int:
    """账户余额 = 期初 + 收入 − 支出（转账已在流水层做成两条腿，不必特殊处理）。"""
    from .accounts import account_balance  # 局部导入：避免模块级循环

    return account_balance(session, account_id)


def goal_saved(session: Session, goal: Goal) -> int:
    """目标已存金额 = 指定账户的**实时余额** + 手工注入之和。

    账户余额可能为负（信用卡）。负数**照实计入**而不是夹到 0：
    夹掉会让"我把首付账户刷爆了"看起来跟"一分没存"一样，
    而前者显然需要更紧急的处理。
    """
    injected = session.scalar(
        select(func.coalesce(func.sum(GoalContribution.amount_minor), 0)).where(
            GoalContribution.goal_id == goal.id
        )
    )
    total = int(injected or 0)
    if goal.account_id is not None:
        total += _account_balance(session, goal.account_id)
    return total


def _refresh_goal_achievement(session: Session, goal: Goal) -> None:
    if goal.status in {"abandoned", "paused", "achieved"}:
        return
    if goal_saved(session, goal) >= goal.target_amount_minor:
        goal.status = "achieved"
        goal.achieved_at = goal.achieved_at or datetime.now()


def inject_bank_into_goal(
    session: Session,
    bank_id: int,
    *,
    goal_id: int | None = None,
    settle_bank: bool = True,
    occurred_at: datetime | str | None = None,
) -> dict[str, Any]:
    """把罐子里的钱一次性注入目标，并可同时结清罐子。

    这是"先攒零钱、攒够了再注入目标"的那一步。罐子与目标**不合并**，
    但可以这样组合（见模块 docstring）。
    """
    bank = _require_bank(session, bank_id)
    target_goal_id = goal_id if goal_id is not None else bank.goal_id
    if target_goal_id is None:
        raise ValidationError("这个罐子没有关联任何储蓄目标", field="goal_id")
    goal = _require_goal(session, target_goal_id)

    amount = balance(session, bank_id)
    if amount <= 0:
        raise ConflictError(
            "罐子是空的，没有可注入的金额",
            entity="piggy_bank",
            entity_id=bank_id,
            balance_minor=amount,
        )

    stamp = _parse_occurred(occurred_at)
    contribute(
        session,
        goal.id,
        amount_minor=amount,
        occurred_at=stamp,
        note=f"来自存钱罐「{bank.name}」",
    )
    if settle_bank:
        session.add(
            PiggyBankDeposit(
                piggy_bank_id=bank_id,
                amount_minor=-amount,
                occurred_at=stamp,
                kind="settle",
                note=f"注入储蓄目标「{goal.name}」",
            )
        )
        session.flush()
        _refresh_achievement(session, bank)

    session.flush()
    return {
        "bank": serialize_bank(session, bank),
        "goal": serialize_goal(session, goal),
        "injected_minor": amount,
    }


# -----------------------------------------------------------------------------
# 序列化
# -----------------------------------------------------------------------------
def serialize_bank(
    session: Session,
    bank: PiggyBank,
    *,
    balance_minor: int | None = None,
    with_eta: bool = True,
) -> dict[str, Any]:
    current = balance(session, bank.id) if balance_minor is None else balance_minor
    payload: dict[str, Any] = {
        "id": bank.id,
        "name": bank.name,
        "target_name": bank.target_name,
        "target_amount_minor": bank.target_amount_minor,
        "balance_minor": current,
        "currency": bank.currency,
        "deadline": bank.deadline.isoformat() if bank.deadline else None,
        "kind": bank.kind,
        "status": bank.status,
        "member_id": bank.member_id,
        "priority": bank.priority,
        "skin": bank.skin,
        "hide_amount": bank.hide_amount,
        "goal_id": bank.goal_id,
        "achieved_at": bank.achieved_at.isoformat() if bank.achieved_at else None,
        "celebrated": bank.celebrated,
        "sort_order": bank.sort_order,
        "note": bank.note,
        "ratio": progress(current, bank.target_amount_minor),
        "milestones": milestones_reached(current, bank.target_amount_minor),
        "deleted_at": bank.deleted_at.isoformat() if bank.deleted_at else None,
    }
    if with_eta:
        payload["eta"] = estimate_completion(session, bank, balance_minor=current)
    rule = get_rule(session, bank.id)
    payload["rule"] = serialize_rule(rule) if rule else None
    return payload


def serialize_rule(rule: PiggyBankRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "id": rule.id,
        "piggy_bank_id": rule.piggy_bank_id,
        "strategy": rule.strategy,
        "enabled": rule.enabled,
        "roundup_unit_minor": rule.roundup_unit_minor,
        "fixed_amount_minor": rule.fixed_amount_minor,
        "percent_bps": rule.percent_bps,
        "category_ids": _load_category_ids(rule),
        "account_id": rule.account_id,
        "deduct_from_account": rule.deduct_from_account,
        "last_run_date": rule.last_run_date.isoformat() if rule.last_run_date else None,
    }


def serialize_deposit(row: PiggyBankDeposit) -> dict[str, Any]:
    return {
        "id": row.id,
        "piggy_bank_id": row.piggy_bank_id,
        "amount_minor": row.amount_minor,
        "occurred_at": row.occurred_at.isoformat(),
        "tz_offset_minutes": row.tz_offset_minutes,
        "source_account_id": row.source_account_id,
        "transaction_id": row.transaction_id,
        "kind": row.kind,
        "note": row.note,
    }


def serialize_goal(session: Session, goal: Goal, *, saved_minor: int | None = None) -> dict[str, Any]:
    saved = goal_saved(session, goal) if saved_minor is None else saved_minor
    # 目标的进度来自**账户实时余额**，而 `status` 只在写入目标时刷新。
    # 账户余额在目标创建之后涨上去时，status 会停在 active ——
    # 于是一个 120% 的目标仍然显示"进行中"，而且**永远不会庆祝**。
    # 因此展示时按当前进度推导一次；刻意不落库，避免"读操作写数据库"。
    status = goal.status
    if status == "active" and saved >= goal.target_amount_minor:
        status = "achieved"
    return {
        "id": goal.id,
        "name": goal.name,
        "target_amount_minor": goal.target_amount_minor,
        "saved_minor": saved,
        "currency": goal.currency,
        "deadline": goal.deadline.isoformat() if goal.deadline else None,
        "kind": goal.kind,
        "status": status,
        "account_id": goal.account_id,
        "member_id": goal.member_id,
        "priority": goal.priority,
        "hide_amount": goal.hide_amount,
        "achieved_at": goal.achieved_at.isoformat() if goal.achieved_at else None,
        "celebrated": goal.celebrated,
        "sort_order": goal.sort_order,
        "note": goal.note,
        "ratio": progress(saved, goal.target_amount_minor),
        "milestones": milestones_reached(saved, goal.target_amount_minor),
        "deleted_at": goal.deleted_at.isoformat() if goal.deleted_at else None,
    }


def bank_detail(session: Session, bank_id: int) -> dict[str, Any]:
    bank = _require_bank(session, bank_id)
    current = balance(session, bank_id)
    return {
        **serialize_bank(session, bank, balance_minor=current),
        "deposits": [serialize_deposit(row) for row in list_deposits(session, bank_id)],
    }
