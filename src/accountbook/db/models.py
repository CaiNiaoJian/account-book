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

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
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
from .base import Base, SoftDeleteMixin, TimestampMixin, local_now, utc_now

__all__ = [
    "Account",
    "AppSetting",
    "AssetSnapshot",
    "AuditLog",
    "CardArtwork",
    "Category",
    "Currency",
    "DailyStat",
    "DayEvent",
    "Institution",
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

    # ---- 展示与卡片（REQ-15 资产卡片墙） ----
    icon: Mapped[str] = mapped_column(String(48), nullable=False, default="accounts")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="accent")
    institution: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="银行/机构名")
    card_no_tail: Mapped[str] = mapped_column(String(8), nullable=False, default="", comment="卡号后四位")

    #: 机构字典键（对应 ``institutions.key``）。用它而不是名称字符串：
    #: 机构改名时卡面配色与 Logo 不该跟着失效。
    brand_key: Mapped[str] = mapped_column(String(32), nullable=False, default="", comment="机构键")
    #: 卡面主题键（对应 ``card_artworks.key``）
    card_style: Mapped[str] = mapped_column(String(32), nullable=False, default="aurora")
    #: 卡组织：``unionpay`` / ``visa`` / ``mastercard`` / ``""``
    card_network: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: 卡面色调微调（覆盖主题的主色；空字符串表示用主题自带色）
    theme_tint: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    # ---- 信用卡相关 ----
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bill_day: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="账单日（1-28）")
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="还款日（1-28）")

    # ---- 行为 ----
    include_in_net_worth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 显示顺序。**卡片墙与账户列表共用这一个字段** ——
    #: 计划里另有一个 ``display_order``，但那会造成"两处顺序不一致"，
    #: 而用户其实只想要一个顺序：拖到哪就是哪。
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
# -----------------------------------------------------------------------------
# 机构与卡面（REQ-15 资产卡片墙）
# -----------------------------------------------------------------------------
class Institution(Base, TimestampMixin, SoftDeleteMixin):
    """机构字典（银行 / 支付渠道 / 券商）。

    为什么存表而不是让用户手打机构名：卡面的配色与标识需要**稳定的键**。
    如果用名称字符串当键，用户把"招商银行"改成"招行"就会让卡面褪色。
    """

    __tablename__ = "institutions"
    __table_args__ = (
        Index("uq_institutions_key_active", "key", unique=True, sqlite_where=text("deleted_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(32), nullable=False, comment="稳定键，如 cmb / alipay")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="bank", comment="bank/wallet/broker/other"
    )
    #: 品牌主色（设计令牌名，不写死色值 —— 日夜主题要能各自校准）
    brand_color: Mapped[str] = mapped_column(String(16), nullable=False, default="accent")
    #: 标识资源的相对路径；为空则用文字缩写
    logo_ref: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class CardArtwork(Base, TimestampMixin):
    """卡面主题。

    ``spec`` 是**自绘卡面的配方**（渐变色标、几何纹理类型、光泽角度），
    而不是一张位图：这样卡面在任意尺寸与 DPI 下都清晰，
    也能随日夜主题自动校准。用户上传的图片走 ``file_ref``。
    """

    __tablename__ = "card_artworks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="稳定键")
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    #: ``builtin`` 内置 / ``uploaded`` 用户上传
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="builtin")
    #: 卡面配方（JSON）：{"stops": [...], "texture": "grain", "sheen": 120, "ink": "light"}
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 上传图片在 ``data/attachments/cards/`` 下的文件名
    file_ref: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(64), nullable=False, default="内置")
    license: Mapped[str] = mapped_column(String(64), nullable=False, default="项目自有")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


# -----------------------------------------------------------------------------
# 日结缓存与快照（REQ-17 / REQ-20 的数据源）
# -----------------------------------------------------------------------------
class DailyStat(Base, TimestampMixin):
    """每日汇总缓存。

    为什么要有缓存表，而不是每次查询都聚合流水：

    1. 日历要一次画 53 周 × 7 天 = 371 天，逐日聚合就是 371 次查询；
    2. ``anomaly_score``（与同星期历史基线的偏离）与 ``entry_state``
       无法从单日流水推出来，必须逐日落库；
    3. ``top_category_id`` 需要"分账优先"的口径，放在聚合里算一次即可。

    权威性说明：**流水是唯一事实来源**，本表只是派生缓存。
    因此每次流水变更都会把受影响区间标记为脏，读取时按需重算
    （见 ``services/daily.py``）—— 缓存与流水不一致时以流水为准。
    """

    __tablename__ = "daily_stats"
    __table_args__ = (
        Index("ix_daily_stats_entry_state", "entry_state"),
        CheckConstraint(
            "entry_state IN ('none', 'logged', 'confirmed')",
            name="entry_state_domain",
        ),
    )

    #: 本地日期（YYYY-MM-DD 的字符串形式由 SQLAlchemy Date 处理）
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    income_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expense_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 收入 - 支出（转账与校准不计入）
    net_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 当日收盘净值（= 当日 asset_snapshots 的净值合计）
    net_worth_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 转账与校准的当日流量（用于"资产变化幅度"指标）
    transfer_in_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transfer_out_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 未清算流水数 —— 参与"部分登记"的判定
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 登记状态：``none`` 无记录 / ``logged`` 有记录 / ``confirmed`` 用户已核对
    entry_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    entry_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    top_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 与"同星期近 8 周基线"的偏离程度（0 = 正常，越大越异常）
    anomaly_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_attachment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 本次重算时刻（UTC）。用于判断缓存新鲜度
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class AssetSnapshot(Base, TimestampMixin):
    """每日每账户余额快照。

    这是**一切时序图与净值曲线的唯一数据源**。不每次全表重算的原因：
    净值曲线需要"每一天的收盘余额"，而这必须按日固化下来 ——
    流水的插入与删除会改变历史某天的余额，所以快照也需要增量重算。
    """

    __tablename__ = "asset_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "account_id", name="uq_asset_snapshots_date_account"),
        Index("ix_asset_snapshots_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 该账户当日收盘净值贡献（= 计入净资产时取 balance，否则 0）
    net_worth_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: ``auto`` 日结自动 / ``manual`` 手动校准
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")


class DayEvent(Base, TimestampMixin, SoftDeleteMixin):
    """当日事件日志（REQ-20）。

    "事件 / 心情 / 纪念日 / 备注 / 待办"共用一张表：
    它们的字段完全一致，差别只在 ``kind``。分成五张表只会让
    "取某天的所有事件"变成五次查询 + 五次合并。
    """

    __tablename__ = "day_events"
    __table_args__ = (
        Index("ix_day_events_date", "date", "sort_order"),
        CheckConstraint(
            "kind IN ('event', 'mood', 'anniversary', 'note', 'todo')",
            name="day_event_kind_domain",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="event")
    title: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 标签名列表（事件不参与流水统计，故不建关联表）
    tags: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    attachments: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


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


# -----------------------------------------------------------------------------
# P1 收尾：周期记账 / 预算 / 债务
# -----------------------------------------------------------------------------
class RecurringRule(Base, TimestampMixin, SoftDeleteMixin):
    """周期记账规则。

    设计要点
    --------
    * **只存"怎么重复"，不预先铺满流水**。把未来 10 年的流水提前写进去是错的：
      改一次规则就要删掉几千行，而且那些行在被生成之前并不是事实。
    * `next_due_date` 是**算出来的缓存**，可以随时由规则 + 上次生成日重建。
      它的存在只是为了让"哪天该提醒"能一次查询拿到，而不是每次全表推算。
    * `auto_post` 区分两种用法：自动记账（如房租）与仅提醒（如信用卡还款 ——
      金额每月不同，替你记反而会记错）。
    """

    __tablename__ = "recurring_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="规则名，如「房租」")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    type: Mapped[str] = mapped_column(String(16), nullable=False, comment="expense/income/transfer")
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    to_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    payee: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    frequency: Mapped[str] = mapped_column(String(12), nullable=False, default="monthly")
    interval: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="每 N 个周期一次")
    #: 每月第几天。``-1`` 表示"月末"—— 直接写 31 会让 2 月永远不触发
    by_month_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    by_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="0=周日")

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    last_posted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    auto_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    generated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Budget(Base, TimestampMixin, SoftDeleteMixin):
    """预算。

    ``period`` 决定期间如何切分；``CUSTOM`` 时才读起止日期。
    ``carryover_minor`` 是**上期结转额**的缓存 —— 由历史期间算出，
    存起来只是为了避免每次打开界面都回溯全部历史。
    """

    __tablename__ = "budgets"
    __table_args__ = (
        # 同一个适用范围只允许一条启用中的预算：允许两条会导致
        # "这个月到底按哪个算"永远说不清，而这正是预算最不该含糊的地方
        Index(
            "uq_budgets_active_scope",
            "scope",
            "category_id",
            unique=True,
            sqlite_where=text("deleted_at IS NULL AND enabled = 1"),
        ),
        CheckConstraint("scope IN ('total', 'category')", name="scope"),
        CheckConstraint(
            "period IN ('weekly', 'monthly', 'quarterly', 'yearly', 'custom')",
            name="period",
        ),
        CheckConstraint("amount_minor >= 0", name="amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(12), nullable=False, default="total")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    period: Mapped[str] = mapped_column(String(12), nullable=False, default="monthly")
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: 未用完的额度是否结转到下一期
    rollover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    carryover_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 用掉多少比例开始提醒。默认 80% 而不是 100% —— 等到超支才说就晚了
    alert_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Debt(Base, TimestampMixin, SoftDeleteMixin):
    """债务 / 债权。

    **同时写入净值**：创建债务时可选地生成一个应收/应付账户
    （``mirror_account_id``），这样"借出去的钱"会出现在资产里、
    "欠别人的钱"会出现在负债里 —— 否则用户会觉得"我借出去 5000，
    净值怎么没变"。
    """

    __tablename__ = "debts"
    __table_args__ = (
        CheckConstraint("kind IN ('lend', 'borrow')", name="kind"),
        CheckConstraint("status IN ('active', 'settled', 'written_off')", name="status"),
        CheckConstraint("principal_minor >= 0", name="principal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, comment="lend=借出 borrow=借入")
    counterparty: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    principal_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    mirror_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, comment="承载这笔债务的应收/应付账户"
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    annual_rate_bps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="年化利率（基点，1bp = 0.01%）"
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    settled_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: 还款方式与期数。**只存这两个参数，不存分摊表** ——
    #: 分摊表完全由它们加本金与利率推出，存一份就要在每次改债务时同步
    repayment_method: Mapped[str] = mapped_column(String(20), nullable=False, default="lump_sum")
    installments: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class DebtPayment(Base, TimestampMixin):
    """债务的还款 / 收款记录。

    ``principal_minor`` 与 ``interest_minor`` **分开存**：
    把本金和利息混成一个数字后，就再也算不出"还剩多少本金"，
    而这正是债务最核心的一个数。
    """

    __tablename__ = "debt_payments"
    __table_args__ = (CheckConstraint("amount_minor >= 0", name="amount"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    debt_id: Mapped[int] = mapped_column(ForeignKey("debts.id"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    principal_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interest_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 业务时间（本地墙上时钟），与流水保持同一套语义
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tz_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, comment="同时记的那笔流水"
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


# -----------------------------------------------------------------------------
# P3：K 线聚合缓存
# -----------------------------------------------------------------------------
class AssetOhlc(Base, TimestampMixin):
    """净值 K 线聚合缓存。

    为什么可以做成缓存：它的每一个数字都能由 ``asset_snapshots`` 推出。
    因此脏标记与 ``daily_stats`` 共用一套 —— 重算日结时顺手删掉重叠的 OHLC 行，
    下次读取自然重算。多一层缓存不增加"口径分叉"的风险，
    因为这里没有任何新的口径，只有聚合。
    """

    __tablename__ = "asset_ohlc"
    __table_args__ = (
        UniqueConstraint("period", "period_start", name="uq_asset_ohlc_period_start"),
        CheckConstraint("period IN ('day', 'week', 'month', 'year')", name="period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: 周期结束日（含）。周/月/年的最后一根 K 线可能不足整期，用它标记真实边界
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    open_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    close_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: "成交量"：周期内的资金流动总额（收入 + 支出）。它不是金额余额，
    #: 而是"这个周期里有多少钱动过" —— 与 K 线图的成交量含义对应
    volume_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


# -----------------------------------------------------------------------------
# P1 收尾：记账模板
# -----------------------------------------------------------------------------
class TransactionTemplate(Base, TimestampMixin, SoftDeleteMixin):
    """记账模板（快捷记账的"常用组合"）。

    存的是**一整套字段**而不是"只存差异"：模板的用途就是"一键填完整个表单"，
    如果只存差异，用户还得自己补齐剩下的字段，那就没省下什么。

    ``tag_ids`` 用 JSON 数组：标签是个短列表，且**只在套用模板时整体读取**，
    为它单开一张关联表会让"套用模板"变成三次查询，而收益只是理论上的规范化。
    真要做标签统计时，走的是 ``transaction_tags``，与这里无关。
    """

    __tablename__ = "transaction_templates"
    __table_args__ = (CheckConstraint("type IN ('expense', 'income', 'transfer')", name="type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="expense")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    to_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="为空表示只填结构，金额每次手输"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=DEFAULT_CURRENCY)
    payee: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tag_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 使用次数与最近使用时间：模板栏按"最常用"排序比按创建时间更符合直觉
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# -----------------------------------------------------------------------------
# P1 尾巴：附件
# -----------------------------------------------------------------------------
class Attachment(Base, TimestampMixin):
    """附件元数据。文件本身落在 ``<data>/attachments/`` 下。

    为什么不存绝对路径：数据目录可能被用户搬走（便携模式、换盘），
    也可能在备份恢复后落到别处。存**相对引用** + 由装配层提供根目录，
    换位置时什么都不用改。

    ``file_ref`` 的内容是 ``<年>/<月>/<内容哈希前 16 位><扩展名>``，
    **不含任何用户可控片段** —— 这是路径穿越的根本防线。
    """

    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("kind IN ('transaction', 'card')", name="kind"),
        CheckConstraint("size_bytes >= 0", name="size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="transaction")
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, index=True
    )
    card_artwork_id: Mapped[int | None] = mapped_column(
        ForeignKey("card_artworks.id"), nullable=True, index=True
    )
    file_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 原始文件名，只用于展示
    original_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 内容哈希：重复上传时用于去重，也让"文件被换过"可被检测
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")


# -----------------------------------------------------------------------------
# P4：存钱罐与储蓄目标
# -----------------------------------------------------------------------------
class PiggyBank(Base, TimestampMixin, SoftDeleteMixin):
    """存钱罐（需求 16）。

    余额**不落库**：由 `piggy_bank_deposits` 求和得出 —— 与流水一致的做法。
    存一个 `balance_minor` 列意味着每笔存入都要记得更新它，
    而总有一次会忘了（导入、插件、手工改库），然后就再也对不上账。
    """

    __tablename__ = "piggy_banks"
    __table_args__ = (
        CheckConstraint("kind IN ('one_time', 'long_term', 'shared')", name="kind"),
        CheckConstraint("status IN ('active', 'achieved', 'paused', 'abandoned')", name="status"),
        CheckConstraint("target_amount_minor > 0", name="target"),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: 要买的东西（"一台相机"），与罐子名分开 —— 罐子名可能是"旅游基金"
    target_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    #: 目标图片（附件表里 kind='piggy' 的那条）。可空：多数罐子用皮肤配色即可
    target_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default=DEFAULT_CURRENCY)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="one_time")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    #: 0–9，数字越大越优先（与列表排序方向一致，避免"1 是最重要"这种反直觉）
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: 罐体外观主题（classic / glass / ceramic / vault …）
    skin: Mapped[str] = mapped_column(String(24), nullable=False, default="classic")
    #: 隐私模式：只显示百分比，不显示金额
    hide_amount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 可选的上级储蓄目标：先攒零钱、够了再一次性注入目标
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"), nullable=True, index=True)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 是否已播放过达成庆祝。分开记是为了"刷新页面不重放礼花"
    celebrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class PiggyBankRule(Base, TimestampMixin):
    """自动归集规则。一个罐子最多一条（`piggy_bank_id` 唯一）。

    一个罐子挂多条规则会让"这笔 3.2 元到底是哪条规则归集的"变得说不清，
    而用户看到罐子里多了一笔钱时**一定**会问这个问题。
    需要多条时建多个罐子 —— 那也是更清晰的账目。
    """

    __tablename__ = "piggy_bank_rules"
    __table_args__ = (
        CheckConstraint(
            "strategy IN ('roundup', 'daily_fixed', 'weekly_fixed', "
            "'income_percent', 'monthly_surplus', 'category_trigger')",
            name="strategy",
        ),
        CheckConstraint("fixed_amount_minor >= 0", name="fixed"),
        CheckConstraint("percent_bps BETWEEN 0 AND 10000", name="percent"),
        CheckConstraint("roundup_unit_minor > 0", name="roundup_unit"),
        UniqueConstraint("piggy_bank_id", name="uq_piggy_bank_rules_bank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    piggy_bank_id: Mapped[int] = mapped_column(ForeignKey("piggy_banks.id"), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(24), nullable=False, default="roundup")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 四舍五入到多少（100 = 补到整元，1000 = 补到整十元）
    roundup_unit_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    #: 定额归集（每日/每周）的金额
    fixed_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 收入百分比，单位为基点（100 = 1%）。用整数存避免浮点误差
    percent_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 分类触发时的分类 id 列表（JSON 数组文本）。派生的多对多，
    #: 与模板的 tag_ids 同样处理：这是一份"配置"而不是需要外键约束的关系
    category_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: 从哪个账户扣。为空且 deduct_from_account 为真时为配置错误，校验时拦住
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    #: 归集时是否同时生成一笔"转账到罐子"的流水（从账户余额里真的扣掉）
    deduct_from_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 上次执行日期。**幂等靠它**：同一天重复触达不会重复归集 ——
    #: 应用可能一天被开关很多次，而"多攒了一笔"用户很难发现
    last_run_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class PiggyBankDeposit(Base, TimestampMixin):
    """罐子的一笔进出。正=存入，负=取出。

    用**一张表 + 带符号金额**而不是"存入表 + 取出表"：罐子余额是求和，
    分成两张表后每次都要相减，而漏掉一边的 bug 会表现为"余额永远偏高"。
    """

    __tablename__ = "piggy_bank_deposits"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('manual', 'auto', 'roundup', 'change', 'milestone', 'withdraw', 'settle')",
            name="kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    piggy_bank_id: Mapped[int] = mapped_column(ForeignKey("piggy_banks.id"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 用户看到的本地墙上时间（与流水同一套双时间语义）
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tz_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    #: 回链到产生这笔存款的流水（四舍五入归集时指向那笔消费）
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Goal(Base, TimestampMixin, SoftDeleteMixin):
    """储蓄目标。

    进度 = （指定账户的**实时余额**）+（手工注入之和）。
    刻意不落库：账户余额本身是从流水聚合出来的，把它的快照再存一份
    会立刻产生"两处不一致时信谁"的问题。
    """

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("kind IN ('purchase', 'emergency', 'travel', 'education', 'other')", name="kind"),
        CheckConstraint("status IN ('active', 'achieved', 'paused', 'abandoned')", name="status"),
        CheckConstraint("target_amount_minor > 0", name="target"),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    target_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default=DEFAULT_CURRENCY)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="purchase")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: 进度来源：该账户的实时余额。为空则只看手工注入
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    hide_amount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    celebrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class GoalContribution(Base, TimestampMixin):
    """对储蓄目标的手工注入（钱不在任何账户里时用）。

    与罐子的存入分开一张表：目标的注入是"往一个数上记账"，
    罐子的存入是"往罐子里放硬币"，两者的界面、语义与取出规则都不同。
    共用一个表会让 `kind` 的取值变成两套语义的并集，
    而每次查询都要额外过滤——那才是真正的重复。
    """

    __tablename__ = "goal_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tz_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# -----------------------------------------------------------------------------
# P5：AI 分析存档
# -----------------------------------------------------------------------------
class AiAnalysis(Base, TimestampMixin):
    """一次 AI 分析的存档。

    **`payload` 存的是实际发出去的脱敏摘要**，存下来是为了将来能回答
    "这次分析是基于什么数据得出的" —— 模型给了个奇怪结论时，
    没有这份 payload 就只能猜。
    """

    __tablename__ = "ai_analyses"
    __table_args__ = (CheckConstraint("source IN ('online', 'offline')", name="source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: online = 模型产出；offline = 离线规则回落
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 实际发送的聚合摘要（已按 redacted 处理）
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 离线回落的原因：no_key / disabled / network / timeout / server / bad_response
    fallback_reason: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    error: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# -----------------------------------------------------------------------------
# P6：工作日日历
# -----------------------------------------------------------------------------
class WorkdayCalendar(Base, TimestampMixin):
    """逐日的工作日覆盖。

    只存**例外**，不存全年 365 天：
    * 周末与工作日的默认规则由代码算（`services/workdays.py`）；
    * 这里存的是"某个本该休息的日子要上班"（调休）与
      "某个本该上班的日子放假"（法定节假日）。

    存全年的坏处不只是体积：一旦铺满了数据，
    "这一天是被明确标记还是默认推断"就再也分不清了，
    而这两者的可信度完全不同。
    """

    __tablename__ = "workday_calendar"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('holiday', 'makeup_workday', 'custom_rest', 'custom_workday')",
            name="kind",
        ),
        CheckConstraint("source IN ('builtin', 'user_import', 'user_edit')", name="source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 日期唯一：一天只能有一个说法，否则"这天到底上不上班"就没有答案
    day: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    is_workday: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: holiday=法定节假日；makeup_workday=调休上班；custom_*=用户自定义
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: 假期名称（春节、国庆…）
    name: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user_import")
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# -----------------------------------------------------------------------------
# P6：发薪
# -----------------------------------------------------------------------------
class PaySource(Base, TimestampMixin, SoftDeleteMixin):
    """薪资来源：一家公司、一份兼职、一笔租金收入。

    **多来源是一等公民**，不是"给同一份工资改个名字"：
    不同来源的入账账户、发薪日、组成项都可能不同，
    而合在一起记会让"我这份工作今年涨了多少"变得无法回答。
    """

    __tablename__ = "pay_sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('salary', 'part_time', 'bonus', 'investment', 'rent', 'other')",
            name="kind",
        ),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="salary")
    employer: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    #: 默认入账账户。为空时收录表要求用户逐次指定 —— 不给默认值比给错的好
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class PayComponent(Base, TimestampMixin):
    """薪资组成模板项：基本工资 / 绩效 / 加班费 / 餐补 / 个税 / 五险一金代扣……

    `calc` 决定金额怎么来：
    * `fixed`   —— 固定金额；
    * `ratio`   —— 按某个基数（应发合计 / 基本工资）的比例；
    * `formula` —— 自定义算式（JSON 描述的表达式树，见 `services/payroll.py`）。

    **公式用 JSON 而不是字符串求值**：`eval()` 会把"我自己的工资表"
    变成一个可执行任意代码的入口，而那份数据是可以被导入的。
    """

    __tablename__ = "pay_components"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('basic', 'performance', 'overtime', 'meal', 'transport', "
            "'bonus', 'commission', 'reimbursement', 'pretax_deduction', "
            "'tax', 'insurance', 'other')",
            name="kind",
        ),
        CheckConstraint("calc IN ('fixed', 'ratio', 'formula')", name="calc"),
        # 加项与减项决定了它进"应发"还是从"应发"里扣
        CheckConstraint("sign IN (1, -1)", name="sign"),
        CheckConstraint("amount_minor >= 0", name="amount"),
        CheckConstraint("rate_bps BETWEEN 0 AND 100000", name="rate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("pay_sources.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    calc: Mapped[str] = mapped_column(String(12), nullable=False, default="fixed")
    #: 1 = 加项（进应发），-1 = 减项（从应发里扣，如个税与五险一金代扣）
    sign: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ratio 的基数：'gross'（应发合计）/ 'basic'（基本工资合计）
    base_key: Mapped[str] = mapped_column(String(16), nullable=False, default="basic")
    #: 万分比，整数存避免浮点误差（1000 = 10%）
    rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: formula 的表达式树（JSON）
    formula: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PaydayRule(Base, TimestampMixin):
    """发薪规则。

    一个规则管一个来源的"什么时候发"。`day` 支持四种写法：
    具体日 / 月末 / 当月末个工作日 / 当月第 N 个工作日。
    """

    __tablename__ = "payday_rules"
    __table_args__ = (
        CheckConstraint(
            "day_kind IN ('fixed', 'month_end', 'last_workday', 'nth_workday')",
            name="day_kind",
        ),
        CheckConstraint("weekend_policy IN ('advance', 'postpone', 'none')", name="weekend_policy"),
        CheckConstraint("holiday_policy IN ('advance', 'postpone', 'none')", name="holiday_policy"),
        CheckConstraint("grace_days BETWEEN 0 AND 30", name="grace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("pay_sources.id"), nullable=False, index=True)
    #: 具体日（1–31）。31 表示"当月最后一天"，与 day_kind='month_end' 等价但更好输入
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    day_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="fixed")
    #: day_kind='nth_workday' 时的 N（第几个工作日）
    nth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 遇周末：提前到之前最近的工作日 / 顺延到之后最近的工作日 / 不调整
    weekend_policy: Mapped[str] = mapped_column(String(12), nullable=False, default="advance")
    #: 遇法定节假日：同上。默认与周末同策略
    holiday_policy: Mapped[str] = mapped_column(String(12), nullable=False, default="advance")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 提前提醒的时刻（HH:MM），空表示不提醒
    remind_at: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    #: 宽限天数：超过这么多天还没填工资收录表就升级提醒
    grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    #: 是否强制填写工资收录表（关闭则只记一笔流水）
    require_form: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class PayrollRecord(Base, TimestampMixin, SoftDeleteMixin):
    """工资收录表的一条记录。

    组成明细用 JSON 存**快照**而不是外键指向 `pay_components`：
    模板会变（涨薪、改比例），而"去年 3 月那笔是怎么算出来的"
    必须能原样复现。指向模板等于让历史随模板一起变。
    """

    __tablename__ = "payroll_records"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'filled', 'skipped')", name="status"),
        CheckConstraint("gross_minor >= 0 AND net_minor >= 0", name="amounts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("pay_sources.id"), nullable=False, index=True)
    #: 归属期间，形如 '2026-10'。用字符串而不是日期：
    #: "10 月的工资"是一个期间概念，硬塞一个日期会在跨月发放时产生歧义
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: 应发合计（各加项之和）
    gross_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 实发 = 应发 − 各减项之和
    net_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 逐项明细快照：[{name, kind, sign, amount_minor, calc, ...}]
    items: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    #: 五险一金代扣快照（P6 后续接入 insurance 时填写）
    insurance_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tax_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 记入账目的流水
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")
    #: 跳过原因（本月跳过必须留痕，见 PLAN 的"合规出口"设计）
    skip_reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# -----------------------------------------------------------------------------
# P6：五险一金
# -----------------------------------------------------------------------------
class InsuranceItem(Base, TimestampMixin):
    """险种字典。**只预置名称，不预置比例**（见模块 docstring）。

    `rate_bps` 用万分比整数存：0.5% = 50，12% = 1200。
    """

    __tablename__ = "insurance_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pension', 'medical', 'unemployment', 'injury', 'maternity', "
            "'housing_fund', 'supplementary_fund', 'enterprise_annuity', "
            "'critical_illness', 'other')",
            name="kind",
        ),
        CheckConstraint("personal_rate_bps BETWEEN 0 AND 10000", name="personal_rate"),
        CheckConstraint("employer_rate_bps BETWEEN 0 AND 10000", name="employer_rate"),
        UniqueConstraint("kind", "city", name="uq_insurance_items_kind_city"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 城市（空表示全国通用 / 用户只维护一套）
    city: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 个人缴纳比例（万分比）。0 表示用户还没填 —— 界面上要提示这一点
    personal_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    employer_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 个人缴纳的部分是否进入个人账户（养老个人部分、医疗个人部分、公积金）
    personal_to_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 单位缴纳的部分是否也进入个人账户（公积金是，养老单位部分不是）
    employer_to_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 保底 / 封顶基数。空表示不设限
    floor_base_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_base_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 该险种是否用公积金基数而不是社保基数
    use_housing_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class InsuranceProfile(Base, TimestampMixin, SoftDeleteMixin):
    """参保档案：谁、在哪个城市、按什么基数缴、从什么时候起。"""

    __tablename__ = "insurance_profiles"
    __table_args__ = (CheckConstraint("social_base_minor >= 0 AND housing_base_minor >= 0", name="bases"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True)
    city: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    employer: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    #: 社保缴纳基数（月）
    social_base_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 公积金缴纳基数（月）。很多城市与社保基数不同，因此分开
    housing_base_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 生效区间。为空表示一直有效
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class InsuranceContribution(Base, TimestampMixin):
    """逐月缴纳记录。

    与 `payroll_records` 一样存**快照**（比例与基数都记下来）：
    比例每年会变，"去年 3 月养老扣了多少、按什么比例"必须能原样复现。
    """

    __tablename__ = "insurance_contributions"
    __table_args__ = (
        CheckConstraint("base_minor >= 0", name="base"),
        CheckConstraint("personal_minor >= 0 AND employer_minor >= 0", name="amounts"),
        UniqueConstraint("profile_id", "period", "item_id", name="uq_insurance_contributions_unique"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("insurance_profiles.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("insurance_items.id"), nullable=False, index=True)
    #: 归属期间，形如 '2026-10'
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    #: 实际使用的基数（已按保底/封顶收敛）
    base_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 未收敛前的基数，用于解释"为什么和我填的不一样"
    raw_base_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 当时使用的比例快照
    personal_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    employer_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    personal_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    employer_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 计入个人账户的金额（个人部分 + 单位划入部分）
    to_account_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 来源：'payroll'（由工资收录表写入）/ 'manual'
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="manual")
    payroll_record_id: Mapped[int | None] = mapped_column(ForeignKey("payroll_records.id"), nullable=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class InsuranceWithdrawal(Base, TimestampMixin):
    """提取记录（购房、租房、退休、医疗、离职销户）。

    余额是算出来的，因此提取也必须是一条记录而不是"改个余额"。
    """

    __tablename__ = "insurance_withdrawals"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('purchase', 'rent', 'retirement', 'medical', 'settlement', 'other')",
            name="reason",
        ),
        CheckConstraint("amount_minor > 0", name="amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("insurance_profiles.id"), nullable=False, index=True)
    #: 从哪个账户提（公积金账户 / 医疗账户 / 养老账户）
    item_id: Mapped[int] = mapped_column(ForeignKey("insurance_items.id"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class InsuranceAnnualStatement(Base, TimestampMixin):
    """年度对账。

    `expected_*` 是用户从**官方对账单**上抄来的数；系统只负责算出差异并展示。
    利息也在这里录 —— 利率因城市与年份而异，我不编（见模块 docstring）。
    """

    __tablename__ = "insurance_annual_statements"
    __table_args__ = (UniqueConstraint("profile_id", "year", name="uq_insurance_statements_year"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("insurance_profiles.id"), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 官方对账单上的全年个人缴纳合计（为空表示还没抄）
    expected_personal_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 官方对账单上的全年单位缴纳合计
    expected_employer_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 官方对账单上的年末账户余额
    expected_balance_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 当年计入的利息（用户录，利率不编）
    interest_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


# -----------------------------------------------------------------------------
# P6：定时任务与提醒
# -----------------------------------------------------------------------------
class ScheduledTask(Base, TimestampMixin):
    """定时任务。

    `rule` 用 JSON 描述"什么时候跑"，字段与 `services/scheduler.py` 的
    解析器一一对应：`frequency`（daily/weekly/monthly/once）、
    `day_of_month` / `day_kind` / `weekend_policy` / `holiday_policy`
    （与发薪规则共用同一套工作日逻辑）、`at`（HH:MM）。

    **不预先铺满执行记录**：`next_run_at` 是算出来的，
    每次执行后由服务层推进。这样"改了规则"立刻生效，
    而不用去清理已经排好的旧实例。
    """

    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('payday', 'insurance', 'bill', 'repayment', 'recurring', "
            "'budget_close', 'report', 'backup', 'custom')",
            name="kind",
        ),
        CheckConstraint(
            "catch_up_policy IN ('startup', 'immediate', 'record_only')",
            name="catch_up",
        ),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority"),
        UniqueConstraint("code", name="uq_scheduled_tasks_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 稳定标识，供代码引用（例如 'payday:1'、'backup:daily'）
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")
    rule: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 错过执行时间后的处理：
    #: startup=启动时补办 / immediate=立即补 / record_only=仅记录不执行
    catch_up_policy: Mapped[str] = mapped_column(String(12), nullable=False, default="startup")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: 业务侧引用（例如发薪任务指向 pay_sources.id）
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class TaskRun(Base, TimestampMixin):
    """每次执行留痕。用于"任务历史与健康度"。"""

    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint("status IN ('success', 'skipped', 'failed', 'caught_up')", name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("scheduled_tasks.id"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 计划执行时刻
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="success")
    #: 结果摘要（例如"生成 1 条工资草稿"）
    summary: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    error: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    #: 是否为"补办"（软件当时没运行）
    caught_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PendingPrompt(Base, TimestampMixin):
    """待办提示（PLAN 里的 `pending_prompts`）。

    强弹与漏填拦截的落点。**必须带合规出口**：
    `snooze_count` / `max_snooze` 限制"稍后提醒"的次数，
    `status='skipped'` 要求先给出原因。只有"必须填"会让真的没工资的
    月份变成死锁，而用户会开始随手填假数据 —— 那比不填更糟。
    """

    __tablename__ = "pending_prompts"
    __table_args__ = (
        CheckConstraint("blocking_level IN ('strong', 'normal')", name="blocking_level"),
        CheckConstraint("status IN ('pending', 'snoozed', 'resolved', 'skipped')", name="status"),
        CheckConstraint("snooze_count >= 0 AND max_snooze >= 0", name="snooze"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="payroll")
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    #: strong = 进入"必须处理"队列；normal = 只作为通知
    blocking_level: Mapped[str] = mapped_column(String(8), nullable=False, default="strong")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    #: 业务对象引用（例如 payroll_records.id）
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snooze_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_snooze: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_remind_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    skip_reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 去重键：同一件事不该反复入队（例如发薪日重复触发）
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)


class Notification(Base, TimestampMixin):
    """通知中心。比提示轻：只读、不拦截。"""

    __tablename__ = "notifications"
    __table_args__ = (CheckConstraint("level IN ('info', 'success', 'warn', 'error')", name="level"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    #: 可选的跳转目标（前端路由）
    action_path: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    action_label: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)


class TransactionRequestKey(Base):
    """Committed together with the transaction; retries return its existing ID."""
    __tablename__ = 'transaction_request_keys'
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_id: Mapped[int] = mapped_column(ForeignKey('transactions.id'), nullable=False)
