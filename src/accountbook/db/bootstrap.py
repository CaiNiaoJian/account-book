"""数据库启动引导 —— 建库、迁移、种子数据，一次做完。

为什么要在应用启动时自动做这三件事
----------------------------------
桌面应用的用户不会去执行 ``alembic upgrade head``。如果升级需要手工命令，
结果必然是"用户升级了版本，打开就报错，然后给了差评"。
因此启动流程固定为：

    连接 → 迁移到最新结构 → 补齐内置数据（幂等）→ 交给服务层

三个步骤都必须是**幂等且可重复**的：每天启动都会走一遍，
而正常路径上它应该只花几毫秒。
"""

from __future__ import annotations

import logging

from .migrations import run_migrations
from .seed import ensure_seed_data
from .session import Database

__all__ = ["bootstrap_database"]

_logger = logging.getLogger(__name__)


def bootstrap_database(database: Database, *, seed: bool = True) -> dict[str, int]:
    """建库 / 迁移 / 播种，返回本次种子新增数量（供日志与测试断言）。

    ``seed=False`` 用于测试：需要一张空库来验证纯逻辑时，
    内置分类反而会干扰断言。
    """
    revision = run_migrations(database)

    counts = {"currencies": 0, "categories": 0, "accounts": 0}
    if seed:
        with database.session() as session:
            counts = ensure_seed_data(session)

    _logger.info(
        "数据库就绪：%s（revision=%s，本次新增 币种%s/分类%s/账户%s）",
        database.path,
        revision or "<empty>",
        counts["currencies"],
        counts["categories"],
        counts["accounts"],
    )
    return counts
