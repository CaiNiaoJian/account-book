"""A populated cache must remain refreshable when Windows TEMP is unavailable."""
import json
import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from accountbook.db.models import DailyStat
from accountbook.services import accounts, daily, transactions


@pytest.mark.skipif(sys.platform != 'win32', reason='Reproduces Windows SQLite temporary-file placement')
@pytest.mark.parametrize('force_file, expected_status', [(True, 500), (False, 200)])
def test_dashboard_refresh_with_unavailable_system_temp(authed_client, tmp_path, force_file, expected_status):
    _, ctx = authed_client
    today = date.today()
    with ctx.database.session() as session:
        cash = accounts.list_accounts(session)[0]
        cash.initial_balance_minor = 123456
        transactions.create_transaction(session, type='expense', account_id=cash.id,
            amount_minor=321, occurred_at=datetime.combine(today, datetime.min.time()))
        session.execute(insert(DailyStat), [
            {'date': today-timedelta(days=index), 'net_worth_minor': 123456,
                'computed_at': datetime(2026, 1, 1)} for index in range(2200)
        ])
        daily.invalidate_all(session)
    # SQLite chooses its Windows temp path in-process. Use a child so this test cannot
    # redirect another test's temporary files, and so the negative control really uses FILE.
    source = Path(__file__).resolve().parents[1] / 'src'
    code = f"import sys; sys.path.insert(0, {str(source)!r})\n" + textwrap.dedent('''
        import json, os
        from fastapi.testclient import TestClient
        from sqlalchemy import event
        from accountbook.api.server import create_app
        from accountbook.api.state import create_context
        from accountbook.config import RuntimeSettings, build_config_store
        from accountbook.db.session import Database
        from accountbook.paths import get_paths
        paths = get_paths()
        config = build_config_store(paths)
        ctx = create_context(paths=paths, settings=RuntimeSettings(single_instance=False, enable_tray=False), config=config)
        ctx.port = 45999
        ctx.database = Database(paths.database)
        if os.environ['FORCE_FILE'] == '1':
            @event.listens_for(ctx.database.engine, 'connect')
            def use_old_temp_store(connection, record):
                connection.execute('PRAGMA temp_store=FILE')
        try:
            with TestClient(create_app(ctx), base_url='http://127.0.0.1:45999', raise_server_exceptions=False) as client:
                client.cookies.set('ab_session', ctx.token.value)
                response = client.get('/api/stats/dashboard')
                result = {'status': response.status_code}
                if response.status_code == 200:
                    data = response.json()
                    result.update(net_worth=data['net_worth']['net_worth_minor'],
                        close=data['trend'][-1]['net_worth_minor'], expense=data['month']['expense_minor'])
                else:
                    result['message'] = response.json()['detail']
                print(json.dumps(result))
        finally:
            ctx.database.dispose()
    ''')
    missing = str(tmp_path / 'unavailable-temp')
    assert not Path(missing).exists()
    environment = {**os.environ, 'TEMP': missing, 'TMP': missing, 'PYTHONIOENCODING': 'utf-8',
        'ACCOUNTBOOK_DATA_DIR': str(ctx.paths.data), 'FORCE_FILE': '1' if force_file else '0'}
    result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout.strip())
    assert response['status'] == expected_status, (response, result.stderr)
    if not force_file:
        assert response == {'status': 200, 'net_worth': 123135, 'close': 123135, 'expense': 321}
        # Return a pooled connection and force a new one: every connection needs the same policy.
        ctx.database.engine.dispose()
        again = authed_client[0].get('/api/stats/dashboard')
        assert again.status_code == 200
        assert again.json()['net_worth']['net_worth_minor'] == 123135
    else:
        assert 'unable to open database file' in result.stderr
