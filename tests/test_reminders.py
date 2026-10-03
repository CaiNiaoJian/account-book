from datetime import datetime, timedelta

from sqlalchemy import func, select

from accountbook.db.models import Notification, Transaction
from accountbook.services import accounts, debts, reminders, scheduler


def test_due_dates_deduplicate_and_resolved_or_skipped_reminders_stay_closed(authed_client):
    client, ctx = authed_client
    now = datetime(2026, 2, 28, 9)
    with ctx.database.session() as session:
        card = accounts.create_account(session, name='Card', type='credit_card', initial_balance_minor=-12345, bill_day=28, due_day=28)
        accounts.create_account(session, name='Archived', type='credit_card', initial_balance_minor=-12345, bill_day=1, due_day=1, is_archived=True)
        debt = debts.create_debt(session, name='Loan', kind='borrow', principal_minor=10000, start_date=now.date()-timedelta(days=30), due_date=now.date())
        assert reminders.generate(session, now=now) == 3
        assert reminders.generate(session, now=now) == 0
        items = reminders.pending(session, now=now)
        assert len(items) == 3
        assert session.scalar(select(func.count()).select_from(Transaction)) == 0
        assert session.scalar(select(func.count()).select_from(Notification)) == 3
        scheduler.resolve_prompt(session, items[0]['id'], now=now)
        scheduler.skip_prompt(session, items[1]['id'], reason='已在其他账户处理', now=now)
        scheduler.snooze_prompt(session, items[2]['id'], minutes=30, now=now)
        assert reminders.pending(session, now=now) == []
        assert len(reminders.pending(session, now=now+timedelta(minutes=31))) == 1
        assert reminders.generate(session, now=now+timedelta(minutes=31)) == 0
        assert card.id and debt.id
    assert client.get('/api/reminders').status_code == 200


def test_future_dates_paid_cards_and_settled_debts_do_not_create_repayment_reminders(authed_client):
    _, ctx = authed_client
    now = datetime(2026, 10, 3, 9)
    with ctx.database.session() as session:
        accounts.create_account(session, name='Paid', type='credit_card', initial_balance_minor=0, due_day=1)
        accounts.create_account(session, name='Future', type='credit_card', initial_balance_minor=-100, bill_day=15, due_day=20)
        debt = debts.create_debt(session, name='Settled', kind='borrow', principal_minor=10000,
            start_date=now.date()-timedelta(days=30), due_date=now.date())
        debt.status = 'settled'
        session.flush()
        assert reminders.generate(session, now=now) == 0
        for kind in ['bill', 'repayment']:
            task = scheduler.upsert_task(session, code=f'test-{kind}', name=kind, kind=kind,
                rule={'frequency': 'daily', 'at': '09:00'}, now=now)
            assert scheduler.run_task_now(session, task.id, now=now)['status'] == 'success'
