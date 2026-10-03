"""Lost responses and concurrent retries produce exactly one committed transaction."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from accountbook.db.models import Transaction, TransactionRequestKey


def test_same_key_survives_response_loss_and_rejects_changed_payload(authed_client):
    client, ctx = authed_client
    payload = {'type': 'expense', 'account_id': 1, 'amount_minor': 1234, 'occurred_at': '2026-10-03T12:00:00'}
    headers = {'Idempotency-Key': 'retry-lost-response'}
    first = client.post('/api/transactions', json=payload, headers=headers)
    second = client.post('/api/transactions', json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()['id'] == second.json()['id']
    changed = client.post('/api/transactions', json={**payload, 'amount_minor': 4321}, headers=headers)
    assert changed.status_code == 409
    with ctx.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 1
        assert session.scalar(select(func.count()).select_from(TransactionRequestKey)) == 1


def test_simultaneous_retries_are_serialized_and_failure_does_not_consume_key(authed_client):
    client, ctx = authed_client
    payload = {'type': 'expense', 'account_id': 1, 'amount_minor': 1234}
    headers = {'Idempotency-Key': 'concurrent-create'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: client.post('/api/transactions', json=payload, headers=headers), range(2)))
    assert [reply.status_code for reply in replies] == [201, 201]
    assert replies[0].json()['id'] == replies[1].json()['id']
    bad = client.post('/api/transactions', json={**payload, 'account_id': 99999}, headers={'Idempotency-Key': 'retry-validation'})
    assert bad.status_code == 404
    valid = client.post('/api/transactions', json=payload, headers={'Idempotency-Key': 'retry-validation'})
    assert valid.status_code == 201
    with ctx.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 2
