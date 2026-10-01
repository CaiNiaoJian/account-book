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
