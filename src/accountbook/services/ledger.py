"""台账（P5 / 需求 21）。

台账与流水的区别
================
流水列表回答"我记了哪些账"；台账回答"**这个账户的钱是怎么变成现在这个数的**"。
因此台账必须有**期初余额**、逐笔的**滚动余额**、以及最后的**期末余额**，
并且期末余额必须与聚合出来的账户余额**严格相等** —— 否则这本台账不可信。

四条与既有实现严格对齐的约定（错一条，数字就对不上）
====================================================
1. **余额公式**：`期初 + Σ(direction='in') − Σ(direction='out')`，
   转账的**转出腿**算在本账户上、**转入腿**（`to_account_id` = 本账户）单独加。
   这与 `services/accounts.py` 的 `_balance_expression()` 是同一个公式。
2. **`status = 'void'` 不计入**（作废的流水不该影响余额），
   但被排除的笔数会**显式报出来** —— 用户记了 10 笔而台账只有 8 笔时，
   必须能知道那 2 笔去哪了，而不是怀疑软件算错了。
3. **软删除不计入**。
4. **`adjust`（调整）计入余额，但不计入收支**：
   它既不是收入也不是支出。所以"所有账户变动之和 = 收入 − 支出"
   这条恒等式**必须加上调整的净额**才成立 —— 漏掉这一项会让体检
   在每次对账之后都报假警。

排序为什么必须带 id 兜底
========================
同一天同一秒记两笔账完全正常（导入、批量录入）。只按 `occurred_at` 排序时，
两行的先后是**数据库返回的顺序**，而它并不保证稳定 —— 于是滚动余额会时对时错，
"期初 + 逐笔 = 期末"也会偶发失败。加上 `id` 作次级键，顺序才确定。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.domain import TransactionSource, TransactionType
from ..core.errors import ValidationError
from ..db.models import Account, Category, Transaction
from .accounts import account_balance, get_account

__all__ = [
    "build_ledger",
    "continuity_check",
    "ledger_entries",
    "opening_balance",
    "reconcile",
    "trial_balance",
]

_logger = logging.getLogger(__name__)

#: 不计入余额的流水状态
_EXCLUDED_STATUS = "void"

#: 既不进收入也不进支出、但会改变余额的类型
_BALANCE_ONLY_TYPES = (TransactionType.ADJUST.value,)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time())


def _day_end(value: date) -> datetime:
    return datetime.combine(value, datetime.max.time())


def signed_minor(direction: str, amount_minor: int) -> int:
    """带符号金额。**唯一的符号来源** —— 别处再写一次 `if direction == 'in'`
    就是第二个真相，迟早会有一处漏改。"""
    return amount_minor if direction == "in" else -amount_minor


def _account_filter(account_id: int):
    """本账户视角：本账户是转出方**或**收到转账的一方。

    漏掉后一半会让台账少算每一笔转入 —— 表现为"转账进来之后余额没变"。
    """
    return (Transaction.account_id == account_id) | (Transaction.to_account_id == account_id)


def _live_conditions(account_id: int, start: datetime | None, end: datetime | None):
    conditions = [
        Transaction.deleted_at.is_(None),
        Transaction.status != _EXCLUDED_STATUS,
        _account_filter(account_id),
    ]
    if start is not None:
        conditions.append(Transaction.occurred_at >= start)
    if end is not None:
        conditions.append(Transaction.occurred_at <= end)
    return conditions


def _rows(
    session: Session, account_id: int, start: datetime | None, end: datetime | None
) -> list[Transaction]:
    """取本账户在区间内的全部流水，按 `(occurred_at, id)` 排序。"""
    statement = (
        select(Transaction)
        .where(*_live_conditions(account_id, start, end))
        .order_by(Transaction.occurred_at, Transaction.id)
    )
    return list(session.scalars(statement).all())


def _net_of(transaction: Transaction, account_id: int) -> int:
    """本账户在**这一行**上的净影响。

    一行既可能是本账户的转出、也可能是本账户收到的转入，
    因此符号取决于"本账户在这行里是 `account_id` 还是 `to_account_id`"。
    """
    if transaction.account_id == account_id:
        return signed_minor(transaction.direction, transaction.amount_minor)
    # 本账户是转入方（转账的另一端）：无条件为正
    return transaction.amount_minor


def opening_balance(session: Session, account_id: int, start: date) -> int:
    """期初余额 = 账户起点余额 + 起始日**之前**的全部净影响。

    用"起点余额 + 之前所有流水"而不是"上一期的期末"：
    后者要求台账连续生成，而用户完全可能只看 6 月不看 5 月。

    求和放在 Python 侧而不是 SQL 里：符号取决于"本账户是这一行的哪一端"，
    写成 SQL 需要一个相关子查询或 join，而正确性远比那点性能重要 ——
    单个账户的历史行数本来就有上界。
    """
    account = get_account(session, account_id)
    rows = _rows(session, account_id, None, _day_start(start) - timedelta(microseconds=1))
    return int(account.initial_balance_minor) + sum(_net_of(row, account_id) for row in rows)


def balance_as_of(session: Session, account_id: int, end: date) -> int:
    """账户在 ``end`` **当日日终**的余额。

    与 `accounts.account_balance` 的区别：后者是**全时段**聚合，
    而台账的期末受区间限制。两者在"存在未来日期的流水"时会不同 ——
    那不是错误（用户可以先记明天的账），但**必须用对的那个来比**，
    否则校验会天天报假警，而假警会让用户再也不看校验。
    """
    account = get_account(session, account_id)
    rows = _rows(session, account_id, None, _day_end(end))
    return int(account.initial_balance_minor) + sum(_net_of(row, account_id) for row in rows)


def future_dated_summary(session: Session, account_id: int, end: date) -> dict[str, int]:
    """``end`` 之后还有多少笔、合计多少。它们**不在**本区间台账里。"""
    rows = _rows(session, account_id, _day_end(end) + timedelta(microseconds=1), None)
    return {
        "count": len(rows),
        "net_minor": sum(_net_of(row, account_id) for row in rows),
    }


def serialize_entry(
    transaction: Transaction,
    account_id: int,
    *,
    signed: int,
    running_balance_minor: int,
    category_by_id: dict[int, Category],
    account_by_id: dict[int, Account],
) -> dict[str, Any]:
    """把一笔流水翻译成台账里的一行（**本账户视角**）。

    同一笔转账，A 账户看到的"对方"是 B，B 看到的"对方"是 A。
    如果直接渲染 `payee`，转账在两边都会显示成空字符串
    （转账通常没有商户），用户就分不清钱去哪了。
    """
    outgoing = transaction.account_id == account_id
    counterparty = ""
    if transaction.type == TransactionType.TRANSFER.value:
        other_id = transaction.to_account_id if outgoing else transaction.account_id
        other = account_by_id.get(int(other_id)) if other_id is not None else None
        counterparty = other.name if other else ""

    category = category_by_id.get(transaction.category_id) if transaction.category_id else None
    return {
        "transaction_id": transaction.id,
        "occurred_at": transaction.occurred_at.isoformat(),
        "tz_offset_minutes": transaction.tz_offset_minutes,
        "type": transaction.type,
        "direction": "in" if signed >= 0 else "out",
        "amount_minor": abs(signed),
        "signed_minor": signed,
        "running_balance_minor": running_balance_minor,
        "payee": transaction.payee,
        "note": transaction.note,
        "status": transaction.status,
        "source": transaction.source,
        "category_id": transaction.category_id,
        "category_name": category.name if category else "",
        "category_kind": category.kind if category else "",
        "to_account_id": transaction.to_account_id,
        # 转账时给出"对方账户"，非转账为空；界面据此显示"→ 储蓄卡"
        "counterparty": counterparty,
        # 'in' 表示这一行在本账户视角下是"收到的转账"
        "leg": "self" if outgoing else "incoming",
        "project_id": transaction.project_id,
        "member_id": transaction.member_id,
        "has_splits": bool(transaction.splits),
    }


def ledger_entries(session: Session, account_id: int, *, start: date, end: date) -> dict[str, Any]:
    """台账正文（期初 + 逐笔 + 期末），**不做校验**。

    校验单独放在 `continuity_check`：读取路径不该因为"发现不一致"就报错，
    用户需要先看到台账本身，再看到它哪里不一致。
    """
    if end < start:
        raise ValidationError("结束日期不能早于起始日期", field="end")
    account = get_account(session, account_id)

    rows = _rows(session, account_id, _day_start(start), _day_end(end))
    category_ids = {row.category_id for row in rows if row.category_id}
    category_by_id = (
        {
            item.id: item
            for item in session.scalars(select(Category).where(Category.id.in_(category_ids))).all()
        }
        if category_ids
        else {}
    )
    account_by_id = {item.id: item for item in session.scalars(select(Account)).all()}

    opening = opening_balance(session, account_id, start)
    balance = opening
    entries: list[dict[str, Any]] = []
    for row in rows:
        delta = _net_of(row, account_id)
        balance += delta
        entries.append(
            serialize_entry(
                row,
                account_id,
                signed=delta,
                running_balance_minor=balance,
                category_by_id=category_by_id,
                account_by_id=account_by_id,
            )
        )

    return {
        "account_id": account_id,
        "account_name": account.name,
        "currency": account.currency,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "opening_balance_minor": opening,
        "closing_balance_minor": balance,
        "entries": entries,
        "count": len(entries),
        "inflow_minor": sum(item["signed_minor"] for item in entries if item["signed_minor"] > 0),
        "outflow_minor": sum(-item["signed_minor"] for item in entries if item["signed_minor"] < 0),
    }


def build_ledger(session: Session, account_id: int, *, start: date, end: date) -> dict[str, Any]:
    """台账 + 连续性校验一起返回（界面一次请求就能画完整页）。"""
    document = ledger_entries(session, account_id, start=start, end=end)
    document["check"] = continuity_check(session, account_id, start=start, end=end, document=document)
    return document


def continuity_check(
    session: Session,
    account_id: int,
    *,
    start: date,
    end: date,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """余额连续性校验：**这本台账的期末是否等于聚合出来的账户余额**。

    两件事分开检查，因为含义不同：

    * `opening + Σ = closing`：台账**内部**自洽（算错了在这里暴露）；
    * `closing == account_balance`：台账与**全局口径**一致
      （聚合漏了一种情况在这里暴露）。

    只看第一条会漏掉"两边用了不同公式"这种最危险的情况：
    内部完全自洽，但和别处对不上。
    """
    document = document or ledger_entries(session, account_id, start=start, end=end)
    recomputed = document["opening_balance_minor"] + sum(item["signed_minor"] for item in document["entries"])
    internal_ok = recomputed == document["closing_balance_minor"]

    # 比的是**截至 end 的余额**，不是全时段余额：见 `balance_as_of` 的说明。
    # 这样无论区间是历史的还是含今天的，期末都必须与它对上 ——
    # 校验无条件成立，不必靠 `covers_today` 分支来豁免。
    bounded = balance_as_of(session, account_id, end)
    aggregate_all = account_balance(session, account_id)
    covers_today = end >= datetime.now().date()
    aggregate_ok = document["closing_balance_minor"] == bounded
    future = future_dated_summary(session, account_id, end)

    excluded_void = session.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.deleted_at.is_(None),
            Transaction.status == _EXCLUDED_STATUS,
            _account_filter(account_id),
            Transaction.occurred_at >= _day_start(start),
            Transaction.occurred_at <= _day_end(end),
        )
    )
    return {
        "internal_ok": internal_ok,
        "aggregate_ok": aggregate_ok,
        "covers_today": covers_today,
        "opening_balance_minor": document["opening_balance_minor"],
        "closing_balance_minor": document["closing_balance_minor"],
        "recomputed_closing_minor": recomputed,
        "aggregate_balance_minor": bounded,
        "all_time_balance_minor": aggregate_all,
        "difference_minor": document["closing_balance_minor"] - bounded,
        # 被排除的作废流水笔数：用户"记了 10 笔、台账只有 8 笔"时要能查到原因
        "excluded_void_count": int(excluded_void or 0),
        # 未来的流水不计入本区间。这不是错误，但用户必须知道它们的存在，
        # 否则他会奇怪"我明明记了那一笔，台账里怎么没有"
        "future_dated_count": future["count"],
        "future_dated_net_minor": future["net_minor"],
        "balanced": internal_ok and aggregate_ok,
    }


def trial_balance(session: Session) -> dict[str, Any]:
    """全局自洽校验（试算平衡）。

    三条恒等式，都值得真的算一遍而不是假设它成立：

    1. **转账对全局净额没有影响**：转出腿 −A、转入腿 +A，合计 0。
       因此"所有账户的变动之和 = 收入 − 支出 **+ 调整净额**"。
       **调整这一项不能漏**：`adjust` 会改变余额但不进收支统计，
       漏掉它会让这个体检在每次对账之后都报假警。
    2. 每条转账的 `to_account_id` 必须与 `account_id` 不同
       （数据库有 CHECK 约束，这里再查一次是为了让**历史数据**也能被审出来 ——
       约束是后来才加的，老数据可能不符合）。
    3. 每条 `transfer` 必须有 `to_account_id`。

    这是个便宜且高效的体检：任何"漏算了一个方向"的 bug 都会让第 1 条失败。
    """
    accounts = list(session.scalars(select(Account)).all())
    balance_changes = sum(
        account_balance(session, account.id) - int(account.initial_balance_minor) for account in accounts
    )

    flows = session.execute(
        select(Transaction.type, func.coalesce(func.sum(Transaction.amount_minor), 0))
        .where(
            Transaction.deleted_at.is_(None),
            Transaction.status != _EXCLUDED_STATUS,
            Transaction.type.in_(
                [
                    TransactionType.INCOME.value,
                    TransactionType.EXPENSE.value,
                    *_BALANCE_ONLY_TYPES,
                ]
            ),
        )
        .group_by(Transaction.type)
    ).all()
    totals = {str(row[0]): int(row[1]) for row in flows}
    income = totals.get(TransactionType.INCOME.value, 0)
    expense = totals.get(TransactionType.EXPENSE.value, 0)

    adjust_rows = session.scalars(
        select(Transaction).where(
            Transaction.deleted_at.is_(None),
            Transaction.status != _EXCLUDED_STATUS,
            Transaction.type.in_(_BALANCE_ONLY_TYPES),
        )
    ).all()
    adjust_net = sum(signed_minor(row.direction, row.amount_minor) for row in adjust_rows)

    broken = list(
        session.scalars(
            select(Transaction.id).where(
                Transaction.deleted_at.is_(None),
                Transaction.type == TransactionType.TRANSFER.value,
                (Transaction.to_account_id.is_(None)) | (Transaction.to_account_id == Transaction.account_id),
            )
        ).all()
    )
    expected = income - expense + adjust_net
    return {
        "account_count": len(accounts),
        "balance_change_minor": balance_changes,
        "income_minor": income,
        "expense_minor": expense,
        "adjust_net_minor": adjust_net,
        "expected_change_minor": expected,
        "transfer_neutral_ok": balance_changes == expected,
        "broken_transfer_ids": [int(item) for item in broken],
        "balanced": balance_changes == expected and not broken,
    }


def reconcile(
    session: Session,
    account_id: int,
    *,
    actual_balance_minor: int,
    as_of: date | None = None,
    note: str = "",
    create_adjustment: bool = True,
) -> dict[str, Any]:
    """对账：拿真实余额（银行 App 上的数）与台账比，差多少就补一笔调整分录。

    为什么生成一笔 `adjust` 流水而不是直接改余额
    -------------------------------------------
    余额是算出来的，没有可以直接改的地方。更重要的是：**差异本身是信息**。
    直接改余额会把"少记了 37.5 元"这件事悄悄抹掉；
    记一笔调整分录则让差异留在账上，用户下次对账还能看到它出现在哪天。
    想撤销这次对账，把那条调整流水作废即可。

    差异为 0 时**不建流水**：留一堆 0 元的调整分录会让台账没法看。
    """
    account = get_account(session, account_id)
    as_of = as_of or datetime.now().date()
    computed = account_balance(session, account_id)
    difference = int(actual_balance_minor) - computed

    result: dict[str, Any] = {
        "account_id": account_id,
        "account_name": account.name,
        "as_of": as_of.isoformat(),
        "computed_balance_minor": computed,
        "actual_balance_minor": int(actual_balance_minor),
        "difference_minor": difference,
        "created_transaction_id": None,
        "new_balance_minor": computed,
    }
    if difference == 0 or not create_adjustment:
        return result

    transaction = Transaction(
        # 调整走 `adjust`：它既不是收入也不是支出，不该污染收支统计
        type=TransactionType.ADJUST.value,
        # 差异为正说明实际比账面多（钱进来了）
        direction="in" if difference > 0 else "out",
        account_id=account_id,
        amount_minor=abs(difference),
        occurred_at=datetime.combine(as_of, datetime.min.time()).replace(hour=23, minute=59),
        payee="余额调整",
        note=note or f"对账调整：账面 {computed} → 实际 {actual_balance_minor}",
        status="cleared",
        source=TransactionSource.MANUAL.value,
    )
    session.add(transaction)
    session.flush()
    result["created_transaction_id"] = transaction.id
    result["new_balance_minor"] = account_balance(session, account_id)
    return result
