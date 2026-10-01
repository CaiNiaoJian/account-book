"""数据库迁移（Alembic）。

对外只暴露 :func:`run_migrations` 一个函数 —— 应用启动时调用它把库升到最新版本。
把它包起来而不是让应用直接调 alembic 命令，是为了：

* 保证迁移作用于**当前账本**（路径由运行时决定），而不是配置里的某个写死路径；
* 让"启动即自动迁移"成为一行代码，用户升级版本后不需要任何手工操作；
* 让测试可以用同一个入口在临时库上验证迁移链。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    from ..session import Database

__all__ = ["MIGRATIONS_DIR", "current_revision", "run_migrations"]

_logger = logging.getLogger(__name__)

#: 迁移脚本所在目录（本包）
MIGRATIONS_DIR: Path = Path(__file__).resolve().parent

#: 仓库根目录 —— alembic.ini 在那里。源码运行时存在；打包后不存在（也不需要）。
_REPO_ROOT = MIGRATIONS_DIR.parents[3]


def _build_config(database: Database) -> Config:
    """构造 Alembic 配置，并把**运行时引擎**注入进去。

    ``config.attributes["engine"]`` 是 Alembic 官方推荐的传参通道，
    env.py 会优先使用它 —— 这样迁移永远作用于正确的那一个数据库文件。
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # 兜底 URL：仅当 env.py 拿不到注入的引擎时才会用到（命令行生成迁移的场景）
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.path}")
    config.attributes["engine"] = database.engine

    ini_path = _REPO_ROOT / "alembic.ini"
    if ini_path.exists():
        # 复用 ini 里的日志配置，避免迁移输出格式与其它日志不一致
        config.config_file_name = str(ini_path)
    return config


def run_migrations(database: Database) -> str:
    """把数据库升级到最新版本，返回升级后的 revision。

    幂等：已经是 head 时不会有任何写操作。
    """
    config = _build_config(database)
    _logger.info("检查数据库结构版本：%s", database.path)
    command.upgrade(config, "head")
    revision = current_revision(database)
    _logger.info("数据库结构已是最新：revision=%s", revision or "<empty>")
    return revision or ""


def current_revision(database: Database) -> str | None:
    """读取当前库的 revision（库为空或未初始化时返回 ``None``）。"""
    from alembic.runtime.migration import MigrationContext

    with database.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        return context.get_current_revision()
