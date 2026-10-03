"""One book, one date: dashboard, overview, calendar, OHLC and ledger agree."""
from datetime import date, datetime, timedelta

import pytest

from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts, daily, kline, ledger, stats, transactions


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "book.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


def test_pages_agree_with_archives_exclusions_adjustments_and_future_entries(db):
    day = date.today()
    with db.session() as session:
        cash, archived, excluded = accounts.list_accounts(session)[:3]
        accounts.update_account(session, cash.id, initial_balance_minor=10000)
        accounts.update_account(session, archived.id, initial_balance_minor=-2000)
        accounts.update_account(session, excluded.id, initial_balance_minor=99999, include_in_net_worth=False)
        usd = accounts.create_account(session, name="Dollar", type="cash", currency="USD", initial_balance_minor=12000)
        for source, kind, amount, direction, target, stamp in [
            (cash, "adjust", 321, "in", None, day),
            (cash, "transfer", 1234, "out", archived.id, day),
            (archived, "expense", 222, "out", None, day),
            (cash, "transfer", 567, "out", excluded.id, day),
            (excluded, "income", 3456, "in", None, day),
            (usd, "expense", 345, "out", None, day),
            (cash, "income", 500000, "in", None, day + timedelta(days=1)),
        ]:
            transactions.create_transaction(session, type=kind, account_id=source.id,
                to_account_id=target, amount_minor=amount, direction=direction, currency=source.currency,
                occurred_at=datetime.combine(stamp, datetime.min.time()).replace(hour=12))
        accounts.update_account(session, archived.id, is_archived=True)
        expected = 7532  # 10000 - 2000 + 321 - 222 - 567
        overview = accounts.overview(session, as_of=day)
        assert archived.id not in {item['id'] for item in overview['accounts']}
        assert overview['net_worth_minor'] == expected
        full = accounts.overview(session, as_of=day, include_archived=True)
        assert full['net_worth_minor'] == expected
        assert {row['currency']: row['net_worth_minor'] for row in full['totals_by_currency']} == {'CNY': expected, 'USD': 11655}
        assert stats.dashboard(session, reference=day)['net_worth']['net_worth_minor'] == expected
        detail = daily.get_day_detail(session, day)
        assert detail['stat']['net_worth_minor'] == detail['net_worth_series'][-1]['net_worth_minor'] == expected
        assert kline.bars(session, period='day', start=day, end=day, warmup=0)[0]['close_minor'] == expected
        for item in full['accounts']:
            assert ledger.balance_as_of(session, item['id'], day) == item['balance_minor']
        assert detail['stat']['income_minor'] == 3456
        assert detail['stat']['expense_minor'] == 222


def test_historical_month_cache_respects_changed_range_boundaries(db):
    with db.session() as session:
        cash = accounts.list_accounts(session)[0]
        accounts.update_account(session, cash.id, initial_balance_minor=10000)
        for day, amount in [(2, 100), (10, 200), (20, 300)]:
            transactions.create_transaction(session, type='income', account_id=cash.id,
                amount_minor=amount, occurred_at=datetime(2025, 1, day, 12))
        for start, end, close, flow in [(1, 31, 10600, 600), (1, 15, 10300, 300),
                (5, 15, 10300, 200), (1, 31, 10600, 600)]:
            result = kline.bars(session, period='month', start=date(2025, 1, start),
                end=date(2025, 1, end), warmup=0)
            assert len(result) == 1
            assert result[0]['period_end'] == date(2025, 1, end).isoformat()
            assert result[0]['close_minor'] == close
            assert result[0]['volume_minor'] == flow
