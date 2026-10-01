"""ORM 模型 —— P1 记账核心闭环的全部数据表。

建模原则
--------
1. **金额一律整数最小单位**（``*_minor`` 后缀，``BigInteger``）。
   看到 ``_minor`` 就知道要配合 :mod:`accountbook.core.money` 使用。
2. **业务时间用本地墙钟**（``occurred_at`` + ``tz_offset_minutes``）；
   审计时间用 UTC。理由见 :mod:`accountbook.db` 的模块说明。
3. **软删除**只加在"业务实体"上（账户/分类/流水/标签/项目/成员），
   审计日志与关联表不需要 —— 它们随主体消失没有独立意义。
4. **不做余额冗余列**：余额由流水聚合得出（见 ``services/accounts.py``）。
   冗余余额在"导入历史数据 / 手工改单 / 崩溃中断"时极易与流水不一致，
   而 SQLite 在几万条流水规模下聚合是毫秒级 —— 用一致性换性能不划算。
5. **枚举存字符串**：SQLite 没有原生枚举，存 ``String`` 并让取值来自
   :mod:`accountbook.core.domain`。这样迁移与跨版本兼容都更稳。
6. **关系加载策略显式声明**：对外序列化要用的关系用 ``selectin``（避免 N+1），
   反向集合关系保持默认惰性（否则一次账户列表查询会级联拉出全部流水）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.domain import (
    AccountType,
    CategoryKind,
    ProjectStatus,
    TransactionSource,
    TransactionStatus,
    TransactionType,
)
from ..core.money import DEFAULT_CURRENCY
from .base import Base, SoftDeleteMixin, TimestampMixin, local_now

__all__ = [
    "Account",
    "AppSetting",
    "AuditLog",
    "Category",
    "Currency",
    "Member",
    "Project",
    "Tag",
    "Transaction",
    "TransactionSplit",
    "transaction_tags",
]


# -----------------------------------------------------------------------------
# 基础字典
# -----------------------------------------------------------------------------
class Currency(Base, TimestampMixin):
    """币种字典。

    存表而不是写死在代码里，是为了让用户能加自己需要的币种，
    并让"最小单位位数"成为**数据**而非假设（日元 0 位、第纳尔 3 位都存在）。
    """

    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(8), primary_key=True, comment="ISO 代码，如 CNY")
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="显示名")
    symbol: Mapped[str] = mapped_column(String(8), nullable=False, default="", comment="符号")
    minor_units: Mapped[int] = mapped_column(Integer, nullable=False, default=2, comment="最小单位位数")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AppSetting(Base, TimestampMixin):
    """应用级键值设置（与"用户偏好"不同：这里放数据侧的元信息）。

    典型用途：``seed_version``（已初始化到哪一版内置数据）、
    ``schema_bootstrapped_at``（首次建库时间）。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


# -----------------------------------------------------------------------------
# 账户
# -----------------------------------------------------------------------------
class Account(Base, TimestampMixin, SoftDeleteMixin):
    """账户（资金容器）。

    ``initial_balance_minor`` 是**建账时的起点余额**，不是当前余额。
    当前余额 = 起点 + 流水净额（含转账与校准），由服务层聚合计算。

    允许负的起点余额：信用卡可能一开始就欠款，应付类账户本身就是负债。
    强行约束符号只会逼用户用"假流水"凑数，反而污染数据。
    """

    __tablename__ = "accounts"
    __table_args__ = (
        # 账户名在同一账本内不应重复（软删除的不算）。
        # 部分唯一索引能表达"仅对未删除行生效"，避免"删了再建同名"被误挡。
        Index(
            "uq_accounts_name_active",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_accounts_sort", "sort_order", "id"),
        Index("ix_accounts_type", "type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False, default=AccountType.CASH.value)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default=DEFAULT_CURRENCY)

    initial_balance_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="建账起点余额（最小单位）"
    )

    # ---- 展示与卡片（REQ-15 资产卡片墙的字段在此预留） ----
    icon: Mapped[str] = mapped_column(String(48), nullable=False, default="accounts")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="accent")
    institution: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="银行/机构名")
    card_no_tail: Mapped[str] = mapped_column(String(8), nullable=False, default="", comment="卡号后四位")

    # ---- 信用卡相关 ----
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bill_day: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="账单日（1-28）")
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="还款日（1-28）")

    # ---- 行为 ----
    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # 反向集合保持惰性：账户列表页不需要拉出全部流水
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="account",
        foreign_keys="Transaction.account_id",
    )


# -----------------------------------------------------------------------------
# 分类 / 标签 / 项目 / 成员
# -----------------------------------------------------------------------------
class Category(Base, TimestampMixin, SoftDeleteMixin):
    """分类（自引用树）。

    ``path`` 是物化路径（如 ``/1/7/23``），用于"取某分类及其所有子分类"这一
    最常见查询 —— 用递归 CTE 也能做，但物化路径在 SQLite 上更快且更易读。
    ``depth`` 冗余子层级，用于限制树深与界面缩进。
    """

    __tablename__ = "categories"
    __table_args__ = (
        Index("ix_categories_parent", "parent_id", "sort_order"),
        Index("ix_categories_kind", "kind"),
        Index("ix_categories_path", "path"),
        CheckConstraint("depth >= 0 AND depth <= 5", name="depth_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=CategoryKind.EXPENSE.value)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(255), nullable=False, default="", comment="物化路径 /1/7/23")
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    icon: Mapped[str] = mapped_column(String(48), nullable=False, default="tag")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="accent")

    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="内置分类：可改可隐藏，但不可删除"
    )
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # 保持惰性：树形接口一次取全表后在内存里组装，比逐级惰性加载快得多
    children: Mapped[list[Category]] = relationship(back_populates="parent")
    parent: Mapped[Category | None] = relationship(back_populates="children", remote_side=[id])


class Tag(Base, TimestampMixin, SoftDeleteMixin):
    """标签 —— 跨分类的横向维度（"出差""报销""旅行"）。"""

    __tablename__ = "tags"
    __table_args__ = (
        Index("uq_tags_name_active", "name", unique=True, sqlite_where=text("deleted_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="teal")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Project(Base, TimestampMixin, SoftDeleteMixin):
    """项目 —— 把一段时期的相关支出归到一处（"装修""毕业旅行"）。"""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="indigo")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ProjectStatus.ACTIVE.value)
    budget_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Member(Base, TimestampMixin, SoftDeleteMixin):
    """成员 —— 分摊与家庭账本的维度（REQ-6）。"""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="purple")
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否为账本主人")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


# -----------------------------------------------------------------------------
# 流水
# -----------------------------------------------------------------------------
#: 流水与标签的多对多关联。
#: 用 ``Table`` 而不是映射类：它没有独立业务字段，也不需要被单独查询。
transaction_tags = Table(
    "transaction_tags",
    Base.metadata,
    Column("transaction_id", ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Transaction(Base, TimestampMixin, SoftDeleteMixin):
    """流水 —— 账本的原子事实。

    金额恒为正数，方向由 ``type`` 决定。允许负数金额会让"这笔是收还是支"
    出现两种等价写法，统计口径随之分叉 —— 从一开始就堵死。
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        # 方向必须与类型自洽：支出只能"减少余额"，收入只能"增加余额"。
        # 转账与校准（adjust）由用户/场景决定方向，故不做限制。
        CheckConstraint(
            "(type = 'expense' AND direction = 'out') "
            "OR (type = 'income' AND direction = 'in') "
            "OR type IN ('transfer', 'adjust')",
            name="direction_matches_type",
        ),
        CheckConstraint("direction IN ('in', 'out')", name="direction_domain"),
        # 转账必须有目标账户，非转账不得有 —— 用约束守住，而不是靠调用方自觉
        CheckConstraint(
            "(type = 'transfer' AND to_account_id IS NOT NULL AND to_account_id <> account_id) "
            "OR (type <> 'transfer' AND to_account_id IS NULL)",
            name="transfer_target",
        ),
        # 三个最常用的访问路径：按时间倒序、按账户、按分类
        Index("ix_transactions_occurred", "occurred_at"),
        Index("ix_transactions_account_occurred", "account_id", "occurred_at"),
        Index("ix_transactions_category_occurred", "category_id", "occurred_at"),
        Index("uq_transactions_external", "external_id", unique=True),
        # 注意：``deleted_at`` 的索引由 SoftDeleteMixin 统一提供，
        # 这里**不要**再建一个 —— 重复索引不会报错，只会白白拖慢每次写入。
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default=TransactionType.EXPENSE.value)
    #: 资金方向。有了它，余额公式简化为
    #: ``SUM(CASE WHEN direction='in' THEN +amount ELSE -amount END)``，
    #: 不必再按类型分支 —— 分支越少，口径出错的机会越少。
    #: 支出恒为 ``out``、收入恒为 ``in``（由 CHECK 约束保证）；
    #: 校准（adjust）由用户在界面选择"增加/减少"。
    direction: Mapped[str] = mapped_column(String(4), nullable=False, default="out")

    #: 业务时间：**本地墙钟**（用户说"今天中午"就是今天中午）
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=local_now, comment="发生时间（本地墙钟）"
    )
    #: 记录时刻的时区偏移（分钟）。用于未来跨时区校正，P1 只存不用。
    tz_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    to_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True, comment="转账目标账户"
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True
    )

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="金额（最小单位，恒正）")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default=DEFAULT_CURRENCY)
    #: 外币流水折算到本位币的金额；P1 固定等于 amount_minor（不做汇率换算）
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    payee: Mapped[str] = mapped_column(String(96), nullable=False, default="", comment="商户/对方")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TransactionStatus.CLEARED.value)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=TransactionSource.MANUAL.value)

    #: 外部唯一标识（导入去重）。SQLite 的 UNIQUE 索引把 NULL 视为互不相同，
    #: 因此手写流水（NULL）不会互相冲突。
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )

    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # 列表页要展示账户名与分类名，用 selectin 一次性取回，避免 N+1
    account: Mapped[Account] = relationship(
        back_populates="transactions", foreign_keys=[account_id], lazy="selectin"
    )
    to_account: Mapped[Account | None] = relationship(foreign_keys=[to_account_id], lazy="selectin")
    category: Mapped[Category | None] = relationship(lazy="selectin")
    project: Mapped[Project | None] = relationship(lazy="selectin")
    member: Mapped[Member | None] = relationship(lazy="selectin")
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tags, lazy="selectin")
    splits: Mapped[list[TransactionSplit]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TransactionSplit.sort_order",
    )


class TransactionSplit(Base, TimestampMixin):
    """分账 —— 一笔流水拆到多个分类。

    只在"一笔支出横跨多个用途"时使用（超市小票里既有食品又有日用品）。
    不变量：同一流水的分账金额之和必须等于流水金额 —— 由服务层校验，
    因为 SQLite 无法用声明式约束表达跨行求和。
    """

    __tablename__ = "transaction_splits"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="split_amount_positive"),
        Index("ix_splits_transaction", "transaction_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category | None] = relationship(lazy="selectin")


# -----------------------------------------------------------------------------
# 审计
# -----------------------------------------------------------------------------
class AuditLog(Base, TimestampMixin):
    """变更审计 —— 回答"这三万块的记录什么时候被改成了三千"。

    不做软删除：审计日志本身就是"删除的痕迹"，删掉它等于自毁证据。
    ``changes`` 只记录**字段级差异**，既不存整行快照（体积），
    也不存用户隐私正文（合规）。
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity", "entity", "entity_id", "created_at"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String(32), nullable=False, comment="表名/实体名")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False, comment="create/update/delete/restore")
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
