"""数据库会话与连接管理。

职责
----
* 创建 SQLite 引擎并设置**必须的 PRAGMA**（见下）；
* 提供会话工厂与"一个工作单元"的上下文管理器；
* 提供 FastAPI 依赖（每请求一个会话）。

为什么 PRAGMA 必须显式设置
--------------------------
SQLite 的默认值对桌面应用并不合适：

======================  ==========================================================
PRAGMA                  作用与选择理由
======================  ==========================================================
``journal_mode=WAL``    写不阻塞读。默认的 DELETE 模式下，"记账时后台正在刷新统计"
                        会出现 ``database is locked``。
``foreign_keys=ON``     **SQLite 默认关闭外键约束**。不打开的话，
                        我们所有 ``ondelete`` 行为与引用完整性都是摆设。
``synchronous=NORMAL``  WAL 下的推荐值：兼顾安全与速度（FULL 会让每笔写入都 fsync）。
``busy_timeout=5000``   遇到锁时等待而不是立刻失败，避免用户看到莫名的写入错误。
======================  ==========================================================

线程模型
--------
FastAPI 的同步端点在线程池中执行，因此每个请求必须有**自己的会话**；
连接由连接池复用但不会并发共用一个连接。``check_same_thread=False``
是前提条件（否则 SQLAlchemy 会在跨线程复用连接时报错）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..core.errors import ValidationError
from .base import Base

__all__ = ["Database", "get_database"]

_logger = logging.getLogger(__name__)


class Database:
    """SQLite 数据库封装。

    刻意做得很薄：它只负责"连接与会话"，不含任何业务查询 ——
    业务规则属于 ``services``，这样它们可以被纯单元测试覆盖。
    """

    def __init__(self, path: Path, *, echo: bool = False) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._gate = threading.Condition()
        self._active = 0
        self._maintenance = False

        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.path}",
            echo=echo,
            future=True,
            # 同步端点在 FastAPI 线程池中运行，连接可能被不同线程取用
            connect_args={"check_same_thread": False, "timeout": 10.0},
        )
        _install_sqlite_pragmas(self.engine)

        self._session_factory = sessionmaker(
            bind=self.engine,
            # 提交后不过期：服务层常在 commit 之后立刻序列化对象返回，
            # 过期会触发额外查询，甚至因会话已关闭而报 DetachedInstanceError。
            expire_on_commit=False,
            autoflush=False,
            future=True,
        )

    # ---- 会话 ---------------------------------------------------------------
    @contextmanager
    def operation(self):
        """Allow concurrent requests; a complete backup waits for active operations to commit."""
        with self._gate:
            self._gate.wait_for(lambda: not self._maintenance)
            self._active += 1
        try:
            yield
        finally:
            with self._gate:
                self._active -= 1
                self._gate.notify_all()

    @contextmanager
    def maintenance(self, *, timeout=30.0):
        with self._gate:
            if not self._gate.wait_for(lambda: not self._maintenance, timeout=timeout):
                raise ValidationError('已有备份正在运行，请稍后重试')
            self._maintenance = True
            if not self._gate.wait_for(lambda: self._active == 0, timeout=timeout):
                self._maintenance = False
                self._gate.notify_all()
                raise ValidationError('等待当前记账保存超时，备份未创建，请稍后重试')
        try:
            yield
        finally:
            with self._gate:
                self._maintenance = False
                self._gate.notify_all()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """一个"工作单元"：正常结束提交，异常回滚。

        服务层统一用它，因此不会出现"某处忘记 commit/rollback"的半提交状态。
        """
        with self.operation(), self._session() as session:
            yield session

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        """创建一个需要调用方自行管理的会话（FastAPI 依赖使用）。"""
        return self._session_factory()

    # ---- 生命周期 -----------------------------------------------------------
    def dispose(self) -> None:
        """关闭连接池（应用退出时调用，确保 WAL 文件被正确收尾）。"""
        self.engine.dispose()

    # ---- 仅测试/工具使用 ----------------------------------------------------
    def create_all(self) -> None:
        """按模型定义直接建表。

        **生产路径不使用它** —— 生产一律走 Alembic 迁移，
        否则后续无法安全地演进表结构。这里只为测试提供快速建库。
        """
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)


def _install_sqlite_pragmas(engine: Engine) -> None:
    """在每次新建底层连接时设置 PRAGMA。"""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: object, _record: object) -> None:  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


#: 进程内单例。桌面应用只有一个数据库文件，无需多实例。
_database: Database | None = None


def get_database(path: Path | None = None) -> Database:
    """取得（或惰性创建）进程内数据库实例。

    显式传 ``path`` 时总是新建 —— 测试需要指向临时文件，
    若沿用单例就会污染真实账本。
    """
    global _database
    if path is not None:
        return Database(path)
    if _database is None:
        from ..paths import get_paths

        _database = Database(get_paths().database)
        _logger.debug("数据库已就绪：%s", _database.path)
    return _database


def reset_database_singleton() -> None:
    """清空单例（测试用）。"""
    global _database
    if _database is not None:
        _database.dispose()
    _database = None
