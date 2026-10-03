"""Editable payroll forms: persistence, exact cents, rollback, and history isolation."""

from datetime import date

import pytest
from sqlalchemy import func, select

from accountbook.db.models import Transaction
from accountbook.services import accounts, daily


@pytest.fixture
def book(authed_client):
    client, ctx = authed_client
    account_id = client.get('/api/accounts').json()[0]['id']
    source = client.post('/api/payroll/sources', json={
        'name': '测试工资', 'account_id': account_id,
    }).json()
    return client, ctx, source


def create_record(client, source):
    response = client.post('/api/payroll/records', json={'source_id': source['id'], 'period': '2026-10'})
    assert response.status_code == 201, response.text
    return response.json()


def components(client, source):
    rows = []
    for name, kind, sign, amount in [('基本工资', 'basic', 1, 1000000), ('个税', 'tax', -1, 10000)]:
        response = client.post('/api/payroll/components', json={
            'source_id': source['id'], 'name': name, 'kind': kind,
            'sign': sign, 'amount_minor': amount,
        })
        assert response.status_code == 201, response.text
        rows.append(response.json())
    return rows


def test_payday_rule_persists_and_keeps_existing_record_dates(book):
    client, _, source = book
    endpoint = f"/api/payroll/sources/{source['id']}/rule"
    assert client.get(endpoint).json() is None
    record = create_record(client, source)
    response = client.put(endpoint, json={
        'day_of_month': 28, 'weekend_policy': 'none', 'holiday_policy': 'none',
        'grace_days': 7, 'remind_at': '18:30',
    })
    assert response.status_code == 200
    assert client.get(endpoint).json()['day_of_month'] == 28
    assert client.get(endpoint).json()['remind_at'] == '18:30'
    resolved = client.get('/api/payroll/pay-date', params={
        'source_id': source['id'], 'period': '2026-10',
    }).json()
    assert resolved['pay_date'] == '2026-10-28'
    assert client.get(f"/api/payroll/records/{record['id']}").json()['pay_date'] == '2026-10-15'
    assert client.get('/api/payroll/sources/99999/rule').status_code == 404
    assert client.put(endpoint, json={'day_of_month': 32}).status_code == 422
    assert client.get(endpoint).json()['day_of_month'] == 28


def test_direct_payroll_creates_exact_income_and_refreshes_cached_day(book):
    client, ctx, source = book
    record = create_record(client, source)
    day = date(2026, 10, 28)
    with ctx.database.session() as session:
        before_balance = accounts.account_balance(session, source['account_id'])
        assert daily.get_day_detail(session, day)['stat']['income_minor'] == 0
    endpoint = f"/api/payroll/records/{record['id']}/fill"
    response = client.post(endpoint, json={
        'gross_minor': 1234567, 'tax_minor': 23456, 'insurance_minor': 12345,
        'pay_date': day.isoformat(),
    })
    assert response.status_code == 200, response.text
    tx_id = response.json()['transaction_id']
    saved = client.get(f"/api/payroll/records/{record['id']}").json()
    assert saved['gross_minor'] == 1234567
    assert saved['net_minor'] == 1198766
    assert saved['insurance_minor'] == 12345
    assert saved['pay_date'] == day.isoformat()
    with ctx.database.session() as session:
        tx = session.get(Transaction, tx_id)
        assert tx.amount_minor == tx.base_amount_minor == 1198766
        assert tx.currency == 'CNY'
        assert tx.occurred_at.date() == day
        assert accounts.account_balance(session, source['account_id']) == before_balance + 1198766
        assert daily.get_day_detail(session, day)['stat']['income_minor'] == 1198766
    # 重复提交不可多记流水，也不可用新填写的金额修改历史。
    repeated = client.post(endpoint, json={'gross_minor': 999999, 'pay_date': '2026-10-01'})
    assert repeated.json()['created'] is False
    assert repeated.json()['transaction_id'] == tx_id
    assert client.get(f"/api/payroll/records/{record['id']}").json() == saved


def test_component_overrides_use_record_snapshot_not_changed_template(book):
    client, _, source = book
    basic, tax = components(client, source)
    record = create_record(client, source)
    client.patch(f"/api/payroll/components/{basic['id']}", json={'amount_minor': 2000000})
    # 只改个税；当月基本工资仍取生成时的快照，不拿更新后的模板重算。
    response = client.post(f"/api/payroll/records/{record['id']}/fill", json={
        'overrides': {str(tax['id']): 12345}, 'pay_date': '2026-10-20',
    })
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/payroll/records/{record['id']}").json()
    assert saved['gross_minor'] == 1000000
    assert saved['net_minor'] == 987655
    assert saved['tax_minor'] == 12345
    current = client.get('/api/payroll/compute', params={'source_id': source['id']}).json()
    assert current['gross_minor'] == 2000000


@pytest.mark.parametrize('payload', [
    {'gross_minor': -1}, {'gross_minor': 1.5}, {'gross_minor': True},
    {'gross_minor': 9007199254740992},
    {'gross_minor': 100, 'tax_minor': 101},
    {'gross_minor': 0}, {'overrides': {'999999': 100}},
])
def test_bad_amounts_leave_draft_and_account_unchanged(book, payload):
    client, ctx, source = book
    record = create_record(client, source)
    response = client.post(f"/api/payroll/records/{record['id']}/fill", json=payload)
    assert response.status_code in (409, 422), response.text
    assert client.get(f"/api/payroll/records/{record['id']}").json() == record
    with ctx.database.session() as session:
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_missing_account_can_be_fixed_without_losing_draft(book):
    client, _, source = book
    client.patch(f"/api/payroll/sources/{source['id']}", json={'account_id': None})
    record = create_record(client, source)
    endpoint = f"/api/payroll/records/{record['id']}/fill"
    assert client.post(endpoint, json={'gross_minor': 123456}).status_code == 422
    assert client.get(f"/api/payroll/records/{record['id']}").json() == record
    client.patch(f"/api/payroll/sources/{source['id']}", json={'account_id': source['account_id']})
    assert client.post(endpoint, json={'gross_minor': 123456}).status_code == 200


def test_insurance_base_edits_keep_recorded_contributions(book):
    client, _, _ = book
    client.put('/api/insurance/items', json={
        'kind': 'housing_fund', 'personal_rate_bps': 725, 'employer_rate_bps': 725,
    })
    profile = client.post('/api/insurance/profiles', json={
        'name': '本人', 'social_base_minor': 1234567, 'housing_base_minor': 876543,
    }).json()
    endpoint = f"/api/insurance/profiles/{profile['id']}"
    recorded = client.post('/api/insurance/contributions', json={
        'profile_id': profile['id'], 'period': '2026-10',
    })
    assert recorded.status_code == 201, recorded.text
    history = client.get('/api/insurance/contributions', params={'profile_id': profile['id']}).json()
    assert client.patch(endpoint, json={'housing_base_minor': 999999, 'social_base_minor': 2345678}).status_code == 200
    assert client.get('/api/insurance/contributions', params={'profile_id': profile['id']}).json() == history
    current = client.get('/api/insurance/compute', params={'profile_id': profile['id']}).json()
    assert current['items'][0]['base_minor'] == 999999
    assert current['personal_total_minor'] == 72500


def test_city_rates_override_generic_rates_once_and_local_disable_wins(book):
    client, _, _ = book
    client.put('/api/insurance/items', json={
        'kind': 'housing_fund', 'personal_rate_bps': 500, 'employer_rate_bps': 500,
    })
    client.put('/api/insurance/items', json={
        'kind': 'housing_fund', 'city': '测试城市', 'personal_rate_bps': 725, 'employer_rate_bps': 725,
    })
    profile = client.post('/api/insurance/profiles', json={
        'name': '城市档案', 'city': '测试城市', 'housing_base_minor': 1000000,
    }).json()
    result = client.get('/api/insurance/compute', params={'profile_id': profile['id']}).json()
    assert len(result['items']) == 1
    assert result['personal_total_minor'] == 72500
    client.put('/api/insurance/items', json={'kind': 'housing_fund', 'city': '测试城市', 'enabled': False})
    result = client.get('/api/insurance/compute', params={'profile_id': profile['id']}).json()
    assert result['items'] == []
