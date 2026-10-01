"""ORM 基类与通用 Mixin。

集中三件事，让模型定义保持"只描述业务字段"：
    1. ``Base`` —— 带**命名约定**的声明式基类（迁移脚本靠它生成稳定约束名）；
    2. :class:`TimestampMixin` —— 审计时间戳；
    3. :class:`SoftDeleteMixin` —— 软删除（配合 ``audit_log`` 形成可追溯的删除）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "SoftDeleteMixin", "TimestampMixin", "local_now", "utc_now"]

#: 约束命名约定。
#:
#: 不设它的话，SQLite 生成的约束名是随机的，Alembic 无法在迁移里可靠地
#: ``drop_constraint`` —— 也就是说"以后想改约束"会变成一件没法做的手术。
#: 这是必须从第一天就定好的基础设施。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """当前 UTC 时间（naive）。

    刻意去掉 tzinfo：SQLite 不保存时区，混用 aware/naive 会在比较时抛
    ``TypeError``。统一为 naive-UTC 后，所有审计时间的比较与排序都是确定的。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def local_now() -> datetime:
    """当前本地墙钟时间（naive），用于业务时间字段的默认值。"""
    return datetime.now()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict[str, object]:
        """把 ORM 实例转成普通字典（仅列字段）。

        仅供调试与日志使用；API 响应一律走显式的 Pydantic 模型 ——
        隐式序列化会把不该外发的字段一起带出去。
        """
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TimestampMixin:
    """审计时间戳（UTC，naive）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        nullable=False,
        comment="创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
        comment="最后修改时间（UTC）",
    )


class SoftDeleteMixin:
    """软删除。

    为什么不用物理删除：账本是"发生过的事实"，误删一笔往往几个月后才发现
    （对账时数字对不上）。保留记录 + 回收站 + 审计日志，才能让用户敢用删除键。
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        default=None,
        nullable=True,
        index=True,
        comment="软删除时间（UTC）；NULL 表示未删除",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        """标记删除（幂等：重复调用不会刷新时间戳，保持"首次删除时刻"可追溯）。"""
        if self.deleted_at is None:
            self.deleted_at = utc_now()

    def restore(self) -> None:
        """从回收站恢复。"""
        self.deleted_at = None
