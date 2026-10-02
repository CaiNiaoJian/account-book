"""Protect existing books, currency totals, lazy caches, and scheduler previews."""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import func, select

from accountbook.core.errors import ValidationError
from accountbook.db.migrations import _build_config, run_migrations
from accountbook.db.models import (
    Account,
    DailyStat,
    Notification,
    PayrollRecord,
    PendingPrompt,
    ScheduledTask,
    TaskRun,
    Transaction,
)
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts, aggregate, daily, payroll, scheduler, transactions
from accountbook.services.scheduler_worker import SchedulerWorker

NOW = datetime(2026, 1, 2, 9)
DAY = NOW.date()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "accountbook.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


def spend(session, account, *, day=DAY, amount=1234):
    return transactions.create_transaction(
        session,
        type="expense",
        account_id=account.id,
        currency=account.currency,
        amount_minor=amount,
        occurred_at=datetime.combine(day, datetime.min.time()),
    )


def test_upgrade_backs_up_committed_wal_and_keeps_records(tmp_path):
    db = Database(tmp_path / "book.db")
    try:
        command.upgrade(_build_config(db), "98dfc6d2c979")
        with db.session() as session:
            account = Account(name="Existing book", type="cash", currency="CNY", initial_balance_minor=12345)
            session.add(account)
            session.flush()
            account_id = account.id
            session.add(
                Transaction(
                    type="expense",
                    direction="out",
                    account_id=account_id,
                    currency="CNY",
                    amount_minor=234,
                    base_amount_minor=234,
                    occurred_at=NOW,
                )
            )
        run_migrations(db)
        with db.session() as session:
            ensure_seed_data(session)
            assert session.get(Account, account_id).initial_balance_minor == 12345
            assert accounts.account_balance(session, account_id) == 12111
        backups = list((tmp_path / "backups").glob("pre-upgrade-*.db"))
        assert len(backups) == 1
        assert list((tmp_path / "backups").iterdir()) == backups
        with sqlite3.connect(backups[0]) as snapshot:
            assert (
                snapshot.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
                == "Existing book"
            )
            assert snapshot.execute("SELECT amount_minor FROM transactions").fetchone()[0] == 234
            assert snapshot.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "98dfc6d2c979"
        run_migrations(db)
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    finally:
        db.dispose()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows packaging script")
def test_application_publication_preserves_portable_book(tmp_path):
    source, target = tmp_path / "staged", tmp_path / "installed"
    for directory in (source, target):
        (directory / "_internal").mkdir(parents=True)
        (directory / "AccountBook.exe").write_bytes(directory.name.encode())
        (directory / "_internal" / "runtime").write_bytes(directory.name.encode())
    data = target / "data"
    data.mkdir()
    (data / "accountbook.db").write_bytes(b"existing ledger with cents")
    (target / "portable.flag").write_bytes(b"portable")
    script = Path(__file__).resolve().parents[1] / "packaging" / "publish_backend.ps1"

    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"$ErrorActionPreference='Stop'; . {quote(script)}; Publish-AccountBook -Source {quote(source)} -Target {quote(target)} -Workspace {quote(tmp_path)}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (data / "accountbook.db").read_bytes() == b"existing ledger with cents"
    assert (target / "portable.flag").read_bytes() == b"portable"
    assert (target / "AccountBook.exe").read_bytes() == b"staged"
    assert (target / "_internal" / "runtime").read_bytes() == b"staged"


def test_two_day_query_does_not_materialize_decades(db):
    with db.session() as session:
        accounts.create_account(session, name="With cents", type="cash", initial_balance_minor=12345)
        daily.ensure_fresh(session, DAY, DAY + timedelta(days=1))
        assert session.scalar(select(func.count()).select_from(DailyStat)) == 2
        assert session.get(DailyStat, DAY).net_worth_minor == 12345


def test_disjoint_caches_stay_invalid_after_partial_refresh(db):
    late = DAY + timedelta(days=20)
    with db.session() as session:
        account = accounts.list_accounts(session)[0]
        daily.ensure_fresh(session, DAY, DAY)
        daily.confirm_day(session, late)
        confirmation = session.get(DailyStat, late).entry_confirmed_at
        spend(session, account)
        daily.ensure_fresh(session, DAY, DAY)
        assert daily._net_worth_at(session, late) == -1234
        assert session.get(DailyStat, late).computed_at.year == 1970
        daily.ensure_fresh(session, late, late)
        assert session.get(DailyStat, late).net_worth_minor == -1234
        assert session.get(DailyStat, late).entry_confirmed_at == confirmation
        assert session.scalar(select(func.count()).select_from(DailyStat)) == 2


def test_restoring_account_invalidates_net_worth(db):
    with db.session() as session:
        account = accounts.create_account(session, name="restore", type="cash", initial_balance_minor=1200)
        accounts.delete_account(session, account.id)
        daily.ensure_fresh(session, DAY, DAY)
        assert session.get(DailyStat, DAY).net_worth_minor == 0
        accounts.restore_account(session, account.id)
        daily.ensure_fresh(session, DAY, DAY)
        assert session.get(DailyStat, DAY).net_worth_minor == 1200


def test_foreign_currencies_are_not_added_as_cny(db):
    with db.session() as session:
        yuan = accounts.create_account(session, name="CNY", type="cash", initial_balance_minor=10000)
        usd = accounts.create_account(
            session, name="USD", type="cash", currency="USD", initial_balance_minor=20000
        )
        yen = accounts.create_account(
            session, name="JPY", type="cash", currency="JPY", initial_balance_minor=30000
        )
        spend(session, yuan, amount=100)
        spend(session, usd, amount=200)
        spend(session, yen, amount=300)
        overview = accounts.overview(session)
        assert overview["net_worth_minor"] == 9900
        assert {row["currency"]: row["net_worth_minor"] for row in overview["totals_by_currency"]} == {
            "CNY": 9900,
            "USD": 19800,
            "JPY": 29700,
        }
        assert transactions.summary(session).expense_minor == 100
        assert aggregate.expense_total(session, start=DAY, end=DAY, currency="USD") == 200
        cell = daily.get_calendar(session, start=DAY, end=DAY)[0]
        assert (cell.expense_minor, cell.net_worth_minor, cell.tx_count) == (100, 9900, 3)
        assert daily.get_day_detail(session, DAY)["stat"]["expense_minor"] == 100
        assert accounts.account_balance(session, usd.id) == 19800
        with pytest.raises(ValidationError, match="相同币种"):
            transactions.create_transaction(
                session, type="transfer", account_id=yuan.id, to_account_id=usd.id, amount_minor=100
            )
        with pytest.raises(ValidationError, match="币种一致"):
            transactions.create_transaction(
                session, type="expense", account_id=yuan.id, currency="USD", amount_minor=100
            )
        with pytest.raises(ValidationError, match="不能更改币种"):
            accounts.update_account(session, yuan.id, currency="USD")


@pytest.mark.parametrize("manual", [False, True])
def test_preview_preserves_business_records_and_task_cursor_after_commit(db, manual):
    with db.session() as session:
        source = payroll.add_source(session, name="Salary", account_id=accounts.list_accounts(session)[0].id)
        task = scheduler.upsert_task(
            session,
            code="pay",
            name="Pay",
            kind="payday",
            ref_id=source.id,
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
        task_id, before = task.id, task.next_run_at
    with db.session() as session:
        result = (
            scheduler.run_task_now(session, task_id, now=NOW, dry_run=True)
            if manual
            else scheduler.run_due(session, now=NOW, dry_run=True)
        )
        assert result["dry_run"] is True
    with db.session() as session:
        for model in (PayrollRecord, PendingPrompt, Notification, TaskRun, Transaction):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        task = session.get(ScheduledTask, task_id)
        assert task.next_run_at == before and task.last_run_at is None
        assert scheduler.run_due(session, now=NOW)["count"] == 1
        assert session.scalar(select(func.count()).select_from(PayrollRecord)) == 1


def test_failed_handler_rolls_back_partial_business_writes(db, monkeypatch):
    def fail(session, task, moment):
        scheduler.notify(session, title="Must roll back", level="info")
        raise ValueError("handler failed")

    monkeypatch.setitem(scheduler._HANDLERS, "custom", fail)
    with db.session() as session:
        scheduler.upsert_task(
            session,
            code="bad",
            name="Bad",
            kind="custom",
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
    with db.session() as session:
        result = scheduler.run_due(session, now=NOW)
        assert result["items"][0]["status"] == "failed"
    with db.session() as session:
        assert scheduler.unread_count(session) == 0
        assert len(scheduler.task_history(session)) == 1


def test_real_execution_respects_outer_transaction_rollback(db):
    with db.session() as session:
        scheduler.upsert_task(
            session,
            code="one",
            name="One",
            kind="custom",
            rule={"frequency": "daily", "at": "09:00"},
            now=NOW - timedelta(days=1),
        )
    with pytest.raises(ValueError, match="outer"), db.session() as session:
        scheduler.run_due(session, now=NOW)
        raise ValueError("outer")
    with db.session() as session:
        assert scheduler.unread_count(session) == 0
        assert scheduler.task_history(session) == []


def test_worker_catches_up_once_and_stops(db):
    yesterday = datetime.now() - timedelta(days=1)
    with db.session() as session:
        scheduler.upsert_task(
            session,
            code="once",
            name="Catch up",
            kind="custom",
            rule={
                "frequency": "once",
                "date": yesterday.date().isoformat(),
                "at": yesterday.strftime("%H:%M"),
            },
            now=yesterday - timedelta(days=1),
        )
    worker = SchedulerWorker(SimpleNamespace(database=db, scheduler_lock=RLock()), interval=3600)
    worker.tick()
    worker.tick()
    with db.session() as session:
        assert len(scheduler.task_history(session)) == 1
        assert scheduler.unread_count(session) == 1
    worker.start()
    worker.stop()
    assert not worker._thread.is_alive()
