"""单实例锁（REQ：稳定性）。

为什么必须有
------------
1. **SQLite 写冲突**：两个进程各持一套缓存写同一个库，会出现
   "我这边刚记的账在那边看不到"的诡异现象。
2. **主题/偏好互相覆盖**：两个进程关闭时先后写 config.json，后写的赢。
3. **端口与令牌混乱**：用户会打开两个窗口却以为是同一个应用。

实现方式
--------
Windows 上使用 ``msvcrt.locking`` 对 ``<data>/app.lock`` 加**独占非阻塞锁**：

* 锁由操作系统在进程退出时自动释放（包括崩溃、被任务管理器结束），
  因此**不会留下需要人工清理的僵尸锁文件**——这是选择文件锁而非
  "写 PID 文件"的根本原因。
* 锁文件内容写入当前 PID 与启动时间，仅用于诊断"是谁占着"。

非 Windows 平台回退到 ``fcntl.flock``（保留跨平台能力，P0 只验证 Windows）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["InstanceLock", "LockResult"]

_logger = logging.getLogger(__name__)

if sys.platform == "win32":  # pragma: no cover - 平台分支
    import msvcrt
else:  # pragma: no cover - 平台分支
    import fcntl


@dataclass(frozen=True, slots=True)
class LockResult:
    """获取锁的结果。

    ``acquired=False`` 时 ``holder`` 里是前一个实例写入的诊断信息。
    """

    acquired: bool
    holder: str | None = None


class InstanceLock:
    """基于文件锁的单实例守卫，支持上下文管理器协议。

    用法::

        lock = InstanceLock(paths.lock_file)
        result = lock.acquire()
        if not result.acquired:
            ...  # 已有实例在运行，提示用户后退出
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._acquired = False

    # ---- 上下文管理 ---------------------------------------------------------
    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # ---- 主要接口 -----------------------------------------------------------
    def acquire(self) -> LockResult:
        """尝试获取独占锁（非阻塞，立即返回）。"""
        if self._acquired:
            return LockResult(acquired=True, holder="self")

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # 无法创建锁文件（目录只读等）：记录后放行，宁可放弃单实例保护
            # 也不能让用户完全打不开程序。
            _logger.warning("单实例锁文件不可用，已跳过单实例检查：%s", exc)
            return LockResult(acquired=True, holder="lock-unavailable")

        try:
            if sys.platform == "win32":
                # LK_NBLCK = 非阻塞独占锁；锁区间取 1 字节即可
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder()
            os.close(fd)
            _logger.info("检测到已有实例正在运行（holder=%s）", holder)
            return LockResult(acquired=False, holder=holder)

        self._fd = fd
        self._acquired = True
        self._write_holder()
        return LockResult(acquired=True)

    def release(self) -> None:
        """释放锁并关闭文件描述符。幂等。"""
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError as exc:
            _logger.debug("释放单实例锁时出现异常（进程退出后系统会回收）：%s", exc)
        finally:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
            self._acquired = False

    # ---- 诊断信息 -----------------------------------------------------------
    def _write_holder(self) -> None:
        if self._fd is None:
            return
        info = f"pid={os.getpid()} since={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.truncate(self._fd, 0)
            os.write(self._fd, info.encode("utf-8"))
            os.fsync(self._fd)
            os.lseek(self._fd, 0, os.SEEK_SET)
        except OSError:
            pass

    def _read_holder(self) -> str | None:
        try:
            content = self._path.read_text(encoding="utf-8", errors="replace").strip()
            return content or None
        except OSError:
            return None
