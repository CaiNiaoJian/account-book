"""周期记账服务（P1 收尾）。

核心决策
--------
1. **只存"怎么重复"，不预先铺流水**。把未来十年提前写进 transactions 是错的：
   改一次规则就得删掉几千行，而且那些行在被生成之前并不是"发生过的事实"。
2. **到期分两种**：``auto_post`` 的自动记账（房租、订阅），
   不勾的只提醒（信用卡还款 —— 金额每月不同，替你记反而会记错）。
3. **补记不能失控**：默认最多补生成 36 期，超出部分只提示不生成。
   一个停了半年的"每日"规则醒来后凭空生成 180 笔流水，
   比不生成更难收拾。
4. **数据为零时一切正常**：没有规则 → 空列表；规则还没到日子 →
   ``upcoming`` 给未来若干天的，界面显示"下一次是 X 月 X 日"。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.domain import RecurringFrequency, TransactionSource, TransactionType
from ..core.errors import NotFoundError, ValidationError
from ..core.periods import add_months, clamp_day, week_start
from ..db.models import Account, Category, RecurringRule
from . import audit
from . import transactions as transactions_service

__all__ = [
    "MAX_CATCH_UP",
    "create_rule",
    "delete_rule",
    "due_rules",
    "get_rule",
    "list_rules",
    "next_occurrence",
    "occurrence_index_hint",
    "post_due",
    "upcoming",
    "update_rule",
]

_logger = logging.getLogger(__name__)

#: 单条规则一次最多补生成多少期。超过就只提醒，不生成 ——
#: 见模块开头的第 3 条理由
MAX_CATCH_UP = 36

_MUTABLE = frozenset(
    {
        "name",
        "enabled",
        "type",
        "account_id",
        "to_account_id",
        "category_id",
        "amount_minor",
        "currency",
        "payee",
        "note",
        "frequency",
        "interval",
        "by_month_day",
        "by_weekday",
        "start_date",
        "end_date",
        "auto_post",
        "lead_days",
    }
)

_FREQUENCIES = {item.value for item in RecurringFrequency}


# -----------------------------------------------------------------------------
# 读取
# -----------------------------------------------------------------------------
def list_rules(session: Session, *, include_disabled: bool = True) -> list[RecurringRule]:
    statement = select(RecurringRule).where(RecurringRule.deleted_at.is_(None))
    if not include_disabled:
        statement = statement.where(RecurringRule.enabled.is_(True))
    return list(session.scalars(statement.order_by(RecurringRule.next_due_date, RecurringRule.id)).all())


def get_rule(session: Session, rule_id: int) -> RecurringRule:
    rule = session.get(RecurringRule, rule_id)
    if rule is None or rule.deleted_at is not None:
        raise NotFoundError("周期规则不存在", entity="recurring_rule", entity_id=rule_id)
    return rule


# -----------------------------------------------------------------------------
# 到期推算（本模块最容易出错的部分，全部集中在两个小函数里）
# -----------------------------------------------------------------------------
def _occurrence(rule: RecurringRule, index: int) -> date:
    """第 ``index`` 次（从 0 开始）的发生日。

    每次都是**从起点重新推算**，而不是"上一次加一个周期"。
    后者会累积误差：1 月 31 日 +1 月 = 2 月 28 日，再 +1 月 = 3 月 28 日 ——
    用户的"每月 31 日"会永久变成 28 日。
    """
    step = max(1, rule.interval or 1)
    frequency = rule.frequency

    if frequency == RecurringFrequency.DAILY.value:
        return rule.start_date + timedelta(days=index * step)

    if frequency == RecurringFrequency.WEEKLY.value:
        base = week_start(rule.start_date) + timedelta(weeks=index * step)
        # by_weekday 缺省用起始日所在星期几，这样"每周三"不需要额外配置
        weekday = rule.by_weekday if rule.by_weekday is not None else rule.start_date.weekday()
        return base + timedelta(days=weekday)

    if frequency == RecurringFrequency.MONTHLY.value:
        shifted = add_months(date(rule.start_date.year, rule.start_date.month, 1), index * step)
        day = rule.by_month_day if rule.by_month_day is not None else rule.start_date.day
        return clamp_day(shifted.year, shifted.month, day)

    if frequency == RecurringFrequency.YEARLY.value:
        day = rule.by_month_day if rule.by_month_day is not None else rule.start_date.day
        return clamp_day(rule.start_date.year + index * step, rule.start_date.month, day)

    raise ValidationError(f"未知重复粒度：{frequency}", field="frequency")


def occurrence_index_hint(rule: RecurringRule, target: date) -> int:
    """给 ``target`` 一个**偏小**的序号估计。

    允许低估（不可能高估），因为 ``next_occurrence`` 会用真实值校正。
    低估只是多循环几次，高估则会漏掉一期 —— 后者是少记一笔账，不可接受。
    """
    step = max(1, rule.interval or 1)
    start = rule.start_date
    frequency = rule.frequency

    if frequency == RecurringFrequency.DAILY.value:
        return max(0, (target - start).days // step)
    if frequency == RecurringFrequency.WEEKLY.value:
        weeks = (target - week_start(start)).days // 7
        return max(0, weeks // step)
    if frequency == RecurringFrequency.MONTHLY.value:
        months = (target.year - start.year) * 12 + (target.month - start.month)
        return max(0, months // step)
    return max(0, (target.year - start.year) // step)


def next_occurrence(rule: RecurringRule, after: date | None = None) -> date | None:
    """严格晚于 ``after`` 的下一个发生日；超出 ``end_date`` 返回 ``None``。

    同时保证不早于 ``start_date`` —— 例如"每月 5 日"、起始日填了 1 月 15 日时，
    第一次应该是 2 月 5 日，而不是已经过去的 1 月 5 日。
    """
    floor_date = rule.start_date if after is None else max(rule.start_date, after + timedelta(days=1))
    index = occurrence_index_hint(rule, floor_date)

    # 循环上限：估计偏小最多偏一个周期，48 次足够覆盖任何夹取情形
    for _ in range(48):
        candidate = _occurrence(rule, index)
        if candidate >= floor_date:
            if rule.end_date is not None and candidate > rule.end_date:
                return None
            return candidate
        index += 1
    return None


def due_rules(session: Session, *, on: date | None = None) -> list[RecurringRule]:
    """已到期（``next_due_date <= on``）的启用规则。"""
    today = on or date.today()
    return [
        rule
        for rule in list_rules(session, include_disabled=False)
        if rule.next_due_date is not None and rule.next_due_date <= today
    ]


def upcoming(session: Session, *, within_days: int = 7, on: date | None = None) -> list[dict[str, Any]]:
    """未来 ``within_days`` 天内即将到期的规则（含逾期未生成的）。

    逾期项也列进来，且 ``overdue`` 标为真 —— 用户需要知道"有一笔该记还没记"，
    而不是让它悄悄从列表里消失。
    """
    today = on or date.today()
    horizon = today + timedelta(days=max(0, within_days))
    items: list[dict[str, Any]] = []
    for rule in list_rules(session, include_disabled=False):
        if rule.next_due_date is None or rule.next_due_date > horizon:
            continue
        items.append(
            {
                "rule": rule,
                "due_date": rule.next_due_date,
                "overdue": rule.next_due_date <= today,
                "days_until": (rule.next_due_date - today).days,
                "auto_post": rule.auto_post,
            }
        )
    items.sort(key=lambda item: item["due_date"])
    return items


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def _validate(payload: dict[str, Any], session: Session) -> None:
    frequency = payload.get("frequency")
    if frequency is not None and frequency not in _FREQUENCIES:
        raise ValidationError(f"未知重复粒度：{frequency}", field="frequency")

    transaction_type = payload.get("type")
    if transaction_type is not None and transaction_type not in {
        TransactionType.EXPENSE.value,
        TransactionType.INCOME.value,
        TransactionType.TRANSFER.value,
    }:
        raise ValidationError(f"周期记账不支持的类型：{transaction_type}", field="type")

    if payload.get("type") == TransactionType.TRANSFER.value and not payload.get("to_account_id"):
        raise ValidationError("转账必须指定目标账户", field="to_account_id")

    interval = payload.get("interval")
    if interval is not None and interval < 1:
        raise ValidationError("间隔至少为 1", field="interval")

    amount = payload.get("amount_minor")
    if amount is not None and amount < 0:
        raise ValidationError("金额不能为负数", field="amount_minor")

    start = payload.get("start_date")
    end = payload.get("end_date")
    if start and end and end < start:
        raise ValidationError("结束日期不能早于起始日期", field="end_date")

    # 账户与分类必须真实存在：让它到生成那天才失败，会让用户白等一个月
    account_id = payload.get("account_id")
    if account_id is not None:
        account = session.get(Account, account_id)
        if account is None or account.deleted_at is not None:
            raise NotFoundError("账户不存在", entity="account", entity_id=account_id)
    to_account_id = payload.get("to_account_id")
    if to_account_id is not None:
        target = session.get(Account, to_account_id)
        if target is None or target.deleted_at is not None:
            raise NotFoundError("目标账户不存在", entity="account", entity_id=to_account_id)
    category_id = payload.get("category_id")
    if category_id is not None:
        category = session.get(Category, category_id)
        if category is None or category.deleted_at is not None:
            raise NotFoundError("分类不存在", entity="category", entity_id=category_id)


def create_rule(session: Session, **payload: Any) -> RecurringRule:
    unknown = set(payload) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的周期字段：{sorted(unknown)}", fields=sorted(unknown))
    _validate(payload, session)

    rule = RecurringRule(**payload)
    rule.next_due_date = next_occurrence(rule, None)
    session.add(rule)
    session.flush()
    audit.record(
        session,
        entity="recurring_rule",
        entity_id=rule.id,
        action="create",
        changes={"name": {"to": rule.name}, "frequency": {"to": rule.frequency}},
    )
    return rule


def update_rule(session: Session, rule_id: int, **changes: Any) -> RecurringRule:
    unknown = set(changes) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的周期字段：{sorted(unknown)}", fields=sorted(unknown))
    rule = get_rule(session, rule_id)

    merged = {field: getattr(rule, field) for field in _MUTABLE}
    merged.update(changes)
    _validate(merged, session)

    before = audit.snapshot(rule, changes.keys())
    for key, value in changes.items():
        setattr(rule, key, value)
    # 改了重复规则就必须重算下次到期日，否则旧日期会一直留着
    if {"frequency", "interval", "start_date", "by_month_day", "by_weekday", "end_date"} & set(changes):
        rule.next_due_date = next_occurrence(rule, rule.last_posted_on)
    session.flush()
    audit.record_diff(
        session,
        entity="recurring_rule",
        entity_id=rule_id,
        action="update",
        before=before,
        after={key: getattr(rule, key) for key in changes},
    )
    return rule


def delete_rule(session: Session, rule_id: int) -> None:
    rule = get_rule(session, rule_id)
    rule.soft_delete()
    session.flush()
    audit.record(session, entity="recurring_rule", entity_id=rule_id, action="delete")


def post_due(
    session: Session, *, on: date | None = None, rule_id: int | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """把到期的规则生成为流水。

    ``dry_run`` 只报告将要生成什么，不写库 —— 界面上的"确认生成"按钮
    需要先让用户看到后果（尤其是补记多期时）。

    返回结构里 ``skipped`` 明确列出被跳过的规则与原因，
    而不是静默跳过：用户看到"3 条规则里有 1 条没生成"时必须能知道为什么。
    """
    today = on or date.today()
    targets = [get_rule(session, rule_id)] if rule_id is not None else due_rules(session, on=today)

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for rule in targets:
        if not rule.enabled:
            skipped.append({"rule_id": rule.id, "name": rule.name, "reason": "disabled"})
            continue
        if not rule.auto_post:
            # 只提醒不记账：交给界面上的"确认记账"
            skipped.append({"rule_id": rule.id, "name": rule.name, "reason": "manual_confirm"})
            continue

        cursor = rule.next_due_date or next_occurrence(rule, rule.last_posted_on)
        produced = 0
        while cursor is not None and cursor <= today and produced < MAX_CATCH_UP:
            created.append(
                {
                    "rule_id": rule.id,
                    "name": rule.name,
                    "date": cursor.isoformat(),
                    "amount_minor": rule.amount_minor,
                    "type": rule.type,
                }
            )
            if not dry_run:
                transactions_service.create_transaction(
                    session,
                    type=rule.type,
                    account_id=rule.account_id,
                    to_account_id=rule.to_account_id,
                    category_id=rule.category_id,
                    amount_minor=rule.amount_minor,
                    currency=rule.currency,
                    occurred_at=datetime.combine(cursor, time(hour=9)),
                    payee=rule.payee,
                    note=rule.note,
                    source=TransactionSource.RECURRING.value,
                )
            rule.last_posted_on = cursor
            rule.generated_count = (rule.generated_count or 0) + 1
            cursor = next_occurrence(rule, cursor)
            produced += 1

        if not dry_run:
            rule.next_due_date = cursor
        if cursor is not None and cursor <= today:
            # 还有没补完的：明确告知，不假装全部完成
            skipped.append(
                {
                    "rule_id": rule.id,
                    "name": rule.name,
                    "reason": "too_many_pending",
                    "limit": MAX_CATCH_UP,
                    "next_due_date": cursor.isoformat(),
                }
            )

    if not dry_run:
        session.flush()
    return {"date": today.isoformat(), "created": created, "skipped": skipped, "dry_run": dry_run}
