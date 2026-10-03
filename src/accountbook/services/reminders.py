"""Visible monthly card bill/due dates and outstanding debt due dates, without posting money."""
from calendar import monthrange
from datetime import date, datetime

from sqlalchemy import select

from ..db.models import PendingPrompt
from . import accounts, debts, scheduler


def _enqueue(session, *, key, kind, title, body, target_kind, target_id, now):
    # A resolved or explicitly skipped item must stay resolved on every subsequent worker tick.
    if session.scalar(select(PendingPrompt.id).where(PendingPrompt.dedupe_key == key)) is not None:
        return False
    scheduler.create_prompt(session, kind=kind, title=title, body=body, target_kind=target_kind,
        target_id=target_id, blocking_level='normal', dedupe_key=key, now=now)
    scheduler.notify(session, level='info', title=title, body=body, action_path='/accounts' if target_kind == 'account' else '/debts',
        action_label='查看并处理', dedupe_key=key, now=now)
    return True


def generate(session, *, now=None, kinds=None):
    moment = now or datetime.now()
    today = moment.date()
    kinds = kinds or {'bill', 'repayment'}
    count = 0
    balances = accounts.balances_by_account(session, as_of=today)
    for account in accounts.list_accounts(session):
        if account.type != 'credit_card':
            continue
        for kind, number, label in [('bill', account.bill_day, '账单核对'), ('repayment', account.due_day, '信用卡还款')]:
            if kind not in kinds or not number or (kind == 'repayment' and balances.get(account.id, 0) >= 0):
                continue
            due = date(today.year, today.month, min(number, monthrange(today.year, today.month)[1]))
            if due > today:
                continue
            count += _enqueue(session, key=f'card:{account.id}:{kind}:{due}', kind=kind,
                title=f'{account.name} · {label}', body=f'{due} 已到，请核对实际账单并处理。提醒不会自动扣款或记账。',
                target_kind='account', target_id=account.id, now=moment)
    if 'repayment' in kinds:
        for debt in debts.list_debts(session, status='active'):
            if debt.due_date is None or debt.due_date > today or debts.debt_status(session, debt)['remaining_minor'] <= 0:
                continue
            count += _enqueue(session, key=f'debt:{debt.id}:{debt.due_date}', kind='repayment',
                title=f'{debt.name} · 到期处理', body=f'{debt.due_date} 已到，请核对还款或收款记录。',
                target_kind='debt', target_id=debt.id, now=moment)
    return count


def pending(session, *, now=None):
    moment = now or datetime.now()
    rows = session.scalars(select(PendingPrompt).where(PendingPrompt.status.in_(['pending', 'snoozed']))
        .order_by(PendingPrompt.id)).all()
    return [scheduler.serialize_prompt(row) for row in rows if row.next_remind_at is None or row.next_remind_at <= moment]
