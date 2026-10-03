"""Restore the whole book in isolation, including WAL, files and unknown preferences."""
import io
import os
import sqlite3
import threading
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from accountbook.config import ConfigStore
from accountbook.core.errors import ValidationError
from accountbook.db.models import Account
from accountbook.db.session import Database
from accountbook.services import accounts, attachments, backups, transactions


def populate(ctx):
    ctx.config.update(theme='dark', language='en-US', backup_enabled=False, custom_example={'retain': True})
    with ctx.database.session() as session:
        account = accounts.list_accounts(session)[0]
        account.initial_balance_minor = 123456
        row = transactions.create_transaction(session, type='expense', account_id=account.id, amount_minor=321,
            occurred_at=datetime(2026, 1, 2, 12))
        attachment = attachments.save(session, root=ctx.paths.attachments, transaction_id=row.id,
            filename='test.pdf', data=b'%PDF-1.7\nexample receipt', kind='transaction')
        return account.id, attachment.file_ref


def test_complete_backup_restore_keeps_records_balance_files_and_settings(authed_client, monkeypatch):
    client, ctx = authed_client
    account_id, ref = populate(ctx)
    body = client.post('/api/backups').json()
    assert body['summary'] == {'transactions': 1, 'accounts': 3, 'attachments': 1}
    preview = client.get(f"/api/backups/{body['name']}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()['settings'] == {'theme': 'dark', 'language': 'en-US'}
    payload = client.get(f"/api/backups/{body['name']}/download").content
    imported = client.post('/api/backups/import', content=payload)
    assert imported.status_code == 200, imported.text
    restored = client.post(f"/api/backups/{imported.json()['name']}/restore")
    assert restored.status_code == 200, restored.text
    root = Path(restored.json()['directory'])
    other = Database(root / 'accountbook.db')
    try:
        with other.session() as session:
            assert accounts.account_balance(session, account_id) == 123135
        assert (root / 'attachments' / ref).read_bytes() == (ctx.paths.attachments / ref).read_bytes()
        config = ConfigStore(root / 'config.json').snapshot()
        assert config.theme == 'dark' and config.language == 'en-US'
        assert config.model_extra['custom_example'] == {'retain': True}
        opened = []
        monkeypatch.setattr('accountbook.api.routes.backups.subprocess.Popen',
            lambda command, **kwargs: opened.append(command))
        assert client.post(f"/api/backups/restored/{root.name}/open").status_code == 200
        assert opened[0][-2:] == ['--data-dir', str(root)]
        with ctx.database.session() as session:
            assert accounts.account_balance(session, account_id) == 123135
    finally:
        other.dispose()


def test_maintenance_timeout_releases_gate_and_corrupt_list_entry_does_not_hide_good_backups(authed_client):
    client, ctx = authed_client
    with ctx.database.session(), pytest.raises(ValidationError, match='超时'), ctx.database.maintenance(timeout=0.01):
        pytest.fail('Backup must wait for pending writes')
    result = backups.create(ctx)
    with zipfile.ZipFile(ctx.paths.backups / 'broken.abk', 'w') as archive:
        archive.writestr('manifest.json', '[]')
    response = client.get('/api/backups')
    assert response.status_code == 200
    assert {item['name'] for item in response.json()['items']} == {result['name'], 'broken.abk'}


def test_backup_includes_uncheckpointed_committed_wal(authed_client):
    _, ctx = authed_client
    with sqlite3.connect(ctx.database.path) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE accounts SET initial_balance_minor=2468 WHERE id=1")
        writer.commit()
        result = backups.create(ctx)
        with zipfile.ZipFile(backups.locate(ctx, result['name'])) as archive:
            data = archive.read('accountbook.db')
        snapshot = ctx.paths.backups / 'snapshot.db'
        snapshot.write_bytes(data)
        with sqlite3.connect(snapshot) as reader:
            assert reader.execute('SELECT initial_balance_minor FROM accounts WHERE id=1').fetchone()[0] == 2468


@pytest.mark.parametrize('name', ['../escape', 'attachments/../escape', 'attachments/C:/x', 'attachments/a\\b', 'attachments/CON.pdf', '/absolute', 'plugins/run.py'])
def test_import_rejects_unsafe_paths_without_writing_outside_staging(authed_client, name):
    client, ctx = authed_client
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(name, b'unsafe')
    assert client.post('/api/backups/import', content=data.getvalue()).status_code == 422
    assert not list(ctx.paths.backups.glob('import-*'))


def test_corrupt_file_and_failed_publication_preserve_current_book(authed_client, monkeypatch):
    _, ctx = authed_client
    account_id, _ = populate(ctx)
    result = backups.create(ctx)
    original = backups.locate(ctx, result['name'])
    corrupt = ctx.paths.backups / 'corrupt.abk'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(corrupt, 'w') as target:
        for name in source.namelist():
            target.writestr(name, b'corrupted' if name == 'config.json' else source.read(name))
    with pytest.raises(ValidationError):
        backups.restore(ctx, corrupt.name)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'rename', fail)
    with pytest.raises(OSError, match='disk full'):
        backups.restore(ctx, original.name)
    assert list((ctx.paths.data / 'restored').iterdir()) == []
    with ctx.database.session() as session:
        assert accounts.account_balance(session, account_id) == 123135


def test_missing_attachment_cannot_produce_successful_backup(authed_client):
    _, ctx = authed_client
    _, ref = populate(ctx)
    (ctx.paths.attachments / ref).unlink()
    with pytest.raises(ValidationError, match='缺少附件'):
        backups.create(ctx)
    assert not list(ctx.paths.backups.glob('manual-*.abk'))


def test_periodic_backup_uses_interval_and_can_be_disabled(authed_client):
    _, ctx = authed_client
    ctx.config.update(backup_enabled=True, backup_interval_hours=24)
    assert backups.scheduled(ctx) is not None
    assert backups.scheduled(ctx) is None
    ctx.config.update(backup_enabled=False)
    assert backups.scheduled(ctx, now=datetime(2099, 1, 1)) is None


def test_backup_waits_for_live_transaction_then_releases_requests(authed_client):
    _, ctx = authed_client
    entered = threading.Event()
    finished = threading.Event()
    result = []
    def backup():
        entered.set()
        result.append(backups.create(ctx))
        finished.set()
    with ctx.database.session() as session:
        session.get(Account, 1).initial_balance_minor = 54321
        thread = threading.Thread(target=backup)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(.05)
    thread.join(timeout=10)
    assert finished.is_set()
    restored = backups.restore(ctx, result[0]['name'])
    with sqlite3.connect(Path(restored['directory']) / 'accountbook.db') as db:
        assert db.execute('SELECT initial_balance_minor FROM accounts WHERE id=1').fetchone()[0] == 54321
