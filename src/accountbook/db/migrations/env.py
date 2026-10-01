"""Alembic 运行环境。

两件事值得注意：

1. **目标库来自我们的 ``Database`` 对象，而不是 alembic.ini**。
   桌面应用的库路径是运行时决定的（安装版 / 便携版 / ``--data-dir`` 三种可能），
   把路径写死在配置里必然出错。因此 :func:`accountbook.db.migrations.run_migrations`
   会通过 ``config.attributes`` 把真实的引擎传进来。

2. **``render_as_batch=True``**。
   SQLite 不支持大多数 ``ALTER TABLE``（改列类型、加约束、删列），
   Alembic 的 batch 模式会「建新表 → 拷数据 → 换名」，这是 SQLite 上
   唯一可行的结构演进方式。不开启的话，未来的迁移会在 SQLite 上直接失败。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Engine, pool

# 必须导入模型模块，否则 Base.metadata 是空的，autogenerate 会认为"要删掉所有表"
from accountbook.db import models  # noqa: F401  （导入即注册）
from accountbook.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _get_engine() -> Engine:
    """取得目标引擎：优先使用调用方注入的，其次回退到配置文件里的 URL。"""
    injected = config.attributes.get("engine")
    if injected is not None:
        return injected  # type: ignore[no-any-return]

    from sqlalchemy import create_engine

    return create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库（用于审查迁移语句）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""
    engine = _get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite 结构演进的关键（见模块说明）
            compare_type=True,  # 列类型变化也要能被 autogenerate 发现
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
