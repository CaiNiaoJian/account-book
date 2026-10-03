"""Execute due tasks at startup and while the application is open."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from . import backups, reminders, scheduler

if TYPE_CHECKING:
    from ..api.state import AppContext

_logger = logging.getLogger(__name__)


class SchedulerWorker:
    def __init__(self, ctx: AppContext, *, interval: float = 30.0) -> None:
        self.ctx = ctx
        self.interval = interval
        self._stop = threading.Event()
        self._last_backup_attempt = 0.0
        self._thread = threading.Thread(target=self._run, name="accountbook-scheduler", daemon=True)

    def tick(self) -> None:
        # Hold the same lock used by manual execution through the transaction commit.
        with self.ctx.scheduler_lock, self.ctx.database.session() as session:
            scheduler.run_due(session)
            reminders.generate(session)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                # Backup must run outside the session: it waits for all live operations to commit.
                if time.monotonic() - self._last_backup_attempt > 3600:
                    self._last_backup_attempt = time.monotonic()
                    try:
                        backups.scheduled(self.ctx)
                    except Exception:
                        _logger.exception("完整自动备份失败，当前账本未受影响")
                        with self.ctx.database.session() as session:
                            scheduler.notify(session, level="warn", title="自动备份未完成",
                                body="请在设置中重试完整备份。当前账本未修改。", action_path="/settings",
                                action_label="备份与恢复", dedupe_key="backup-failed")
            except Exception:
                _logger.exception("自动执行到期任务失败，将在下次检查时重试")
            if self._stop.wait(self.interval):
                break
