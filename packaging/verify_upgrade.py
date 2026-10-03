"""Run the packaged app against a populated previous-schema book in isolation."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from alembic import command

from accountbook.config import ConfigStore
from accountbook.db.migrations import _build_config
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts, attachments, transactions


def verify(executable: Path):
    with tempfile.TemporaryDirectory(prefix='upgrade-', dir='.smoke-test') as temporary:
        root = Path(temporary).resolve()
        database = Database(root / 'accountbook.db')
        try:
            command.upgrade(_build_config(database), '6b308efaafa3')
            with database.session() as session:
                ensure_seed_data(session)
            with database.session() as session:
                cash = accounts.list_accounts(session)[0]
                cash.initial_balance_minor = 123456
                row = transactions.create_transaction(session, type='expense', account_id=cash.id,
                    amount_minor=321, occurred_at=datetime.now())
                attachment = attachments.save(session, root=root / 'attachments', transaction_id=row.id,
                    filename='upgrade.pdf', data=b'%PDF-1.7\nupgrade preservation', kind='transaction')
                record_id, file_ref = row.id, attachment.file_ref
        finally:
            database.dispose()
        ConfigStore(root / 'config.json').update(theme='dark', language='en-US', backup_enabled=False,
            custom_upgrade_value={'keep': True})
        with (root / 'stdout.txt').open('w') as stdout, (root / 'stderr.txt').open('w') as stderr:
            process = subprocess.Popen([str(executable.resolve()), '--serve-only', '--data-dir', str(root)],
                stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic() + 90
                ready = False
                while process.poll() is None and time.monotonic() < deadline:
                    log = root / 'logs' / 'accountbook.log'
                    if log.exists() and '界面入口：' in log.read_text(encoding='utf-8'):
                        ready = True
                        break
                    time.sleep(0.2)
                if not ready:
                    raise RuntimeError('Packaged upgrade did not reach a ready local service')
                with closing(sqlite3.connect(root / 'accountbook.db')) as db:
                    assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '7a3d2e910001'
                    assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1
                    assert db.execute('SELECT amount_minor FROM transactions WHERE id=?', (record_id,)).fetchone()[0] == 321
                    assert db.execute('SELECT initial_balance_minor FROM accounts WHERE id=1').fetchone()[0] == 123456
                assert (root / 'attachments' / file_ref).read_bytes() == b'%PDF-1.7\nupgrade preservation'
                prefs = json.loads((root / 'config.json').read_text(encoding='utf-8'))
                assert prefs['theme'] == 'dark' and prefs['language'] == 'en-US'
                assert prefs['custom_upgrade_value'] == {'keep': True}
                assert list((root / 'backups').glob('pre-upgrade-*.db'))
                print('Packaged upgrade preserved records, balances, attachments and settings; pre-upgrade snapshot exists.')
            finally:
                process.terminate()
                process.wait(timeout=20)


if __name__ == '__main__':
    verify(Path(sys.argv[1]))
