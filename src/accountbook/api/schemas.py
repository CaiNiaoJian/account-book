"""API 请求 / 响应模型（Pydantic v2）。

约定
----
* **金额字段一律以 ``_minor`` 结尾且为 ``int``**：接口契约本身就拒绝浮点，
  前端也不可能"不小心"传一个 12.34 过来。
* 请求模型只暴露**可写字段**（与服务层的白名单一致），
  响应模型可以更宽 —— 但绝不包含用户不该看到的内部字段。
* ``model_config = ConfigDict(from_attributes=True)`` 让响应模型能直接从
  ORM 对象构造，省掉一层手写转换（也就少了一处会忘记同步的地方）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AccountCreate",
    "AccountOut",
    "AccountOverviewOut",
    "AccountUpdate",
    "CardArtworkCreate",
    "CardArtworkOut",
    "CardArtworkUpdate",
    "CardUpdate",
    "CategoryCreate",
    "CategoryMergeResult",
    "CategoryMoveRequest",
    "CategoryNode",
    "CategoryOut",
    "CategoryUpdate",
    "CurrencyOut",
    "DayEventCreate",
    "DayEventOut",
    "EnumsOut",
    "InstitutionCreate",
    "InstitutionOut",
    "InstitutionUpdate",
    "MemberCreate",
    "MemberOut",
    "MemberUpdate",
    "ProjectCreate",
    "ProjectOut",
    "ProjectUpdate",
    "ReorderRequest",
    "SplitIn",
    "SplitOut",
    "TagCreate",
    "TagOut",
    "TagUpdate",
    "TransactionCreate",
    "TransactionListOut",
    "TransactionOut",
    "TransactionUpdate",
]


class RequestModel(BaseModel):
    """所有**请求**模型的基类。

    ``extra="forbid"``：未知字段一律 422，而不是静默忽略。

    为什么不让未知字段"宽容通过"：对写错了字段名的调用方来说，
    静默忽略意味着"请求成功但改动没发生" —— 这类问题在界面上表现为
    "我明明改了却没生效"，排查成本极高。宁可让它在开发期就大声失败。
    响应模型不受此约束（后端新增字段不应让老前端报错）。
    """

    model_config = ConfigDict(extra="forbid")


# -----------------------------------------------------------------------------
# 账户
# -----------------------------------------------------------------------------
class AccountBase(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = "cash"
    currency: str = "CNY"
    initial_balance_minor: int = 0
    icon: str = "accounts"
    color: str = "accent"
    institution: str = ""
    card_no_tail: str = ""
    credit_limit_minor: int = 0
    bill_day: int | None = None
    due_day: int | None = None
    include_in_net_worth: bool = True
    is_archived: bool = False
    sort_order: int = 100
    note: str = ""


class AccountCreate(AccountBase):
    pass


class AccountUpdate(RequestModel):
    """局部更新：所有字段可选，只发改动项。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    type: str | None = None
    currency: str | None = None
    initial_balance_minor: int | None = None
    icon: str | None = None
    color: str | None = None
    institution: str | None = None
    card_no_tail: str | None = None
    credit_limit_minor: int | None = None
    bill_day: int | None = None
    due_day: int | None = None
    include_in_net_worth: bool | None = None
    is_archived: bool | None = None
    sort_order: int | None = None
    note: str | None = None
    meta: dict[str, Any] | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    currency: str
    initial_balance_minor: int
    icon: str
    color: str
    institution: str
    card_no_tail: str
    credit_limit_minor: int
    bill_day: int | None
    due_day: int | None
    include_in_net_worth: bool
    is_archived: bool
    sort_order: int
    note: str
    deleted_at: datetime | None = None


class AccountOverviewOut(BaseModel):
    """资产总览：账户明细 + 汇总。"""

    accounts: list[dict[str, Any]]
    assets_minor: int
    liabilities_minor: int
    net_worth_minor: int
    account_count: int
    counted_in_net_worth: int


# -----------------------------------------------------------------------------
# 分类
# -----------------------------------------------------------------------------
class CategoryCreate(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["expense", "income", "transfer"] = "expense"
    parent_id: int | None = None
    icon: str = "tag"
    color: str = "accent"
    sort_order: int = 100
    note: str = ""


class CategoryUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    icon: str | None = None
    color: str | None = None
    is_hidden: bool | None = None
    sort_order: int | None = None
    note: str | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: str
    parent_id: int | None
    path: str
    depth: int
    icon: str
    color: str
    is_system: bool
    is_hidden: bool
    sort_order: int
    note: str
    deleted_at: datetime | None = None


class CategoryNode(CategoryOut):
    """树节点（带子节点）。"""

    children: list[CategoryNode] = Field(default_factory=list)


class CategoryMoveRequest(RequestModel):
    parent_id: int | None = Field(default=None, description="新的父分类；None 表示移到根级")


class CategoryMergeResult(BaseModel):
    transactions: int
    splits: int
    children: int


# -----------------------------------------------------------------------------
# 流水
# -----------------------------------------------------------------------------
class SplitIn(RequestModel):
    category_id: int | None = None
    amount_minor: int = Field(gt=0, description="分账金额，整数最小单位")
    note: str = ""
    sort_order: int = 0


class SplitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int | None
    amount_minor: int
    note: str


class TransactionCreate(RequestModel):
    type: Literal["expense", "income", "transfer", "adjust"] = "expense"
    direction: Literal["in", "out"] | None = Field(
        default=None, description="资金方向；收入/支出会自动确定，仅校准需要显式指定"
    )
    occurred_at: datetime | None = None
    account_id: int
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int = Field(gt=0, description="金额，整数最小单位（恒为正）")
    currency: str | None = None
    payee: str = ""
    note: str = ""
    status: Literal["pending", "cleared", "reconciled", "void"] | None = None
    source: str | None = None
    external_id: str | None = None
    project_id: int | None = None
    member_id: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    splits: list[SplitIn] | None = None
    #: 标签 id 列表。传 ``[]`` 表示不带标签；省略表示不改动（更新时）
    tag_ids: list[int] | None = None

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        """金额必须是整数。

        显式拦截浮点：``12.34`` 会被 Pydantic 的 int 校验拒绝并给出难懂的报错，
        这里提前给出可操作的提示（"请传最小单位整数"）。
        """
        if isinstance(value, float):
            raise ValueError("金额必须是最小单位整数（分），不接受浮点数；例如 12.34 元应传 1234")
        return value


class TransactionUpdate(RequestModel):
    type: Literal["expense", "income", "transfer", "adjust"] | None = None
    direction: Literal["in", "out"] | None = None
    occurred_at: datetime | None = None
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = None
    payee: str | None = None
    note: str | None = None
    status: Literal["pending", "cleared", "reconciled", "void"] | None = None
    source: str | None = None
    external_id: str | None = None
    project_id: int | None = None
    member_id: int | None = None
    meta: dict[str, Any] | None = None
    #: 传 ``[]`` 清空分账；省略（None）表示不改动分账
    splits: list[SplitIn] | None = None
    #: 传 ``[]`` 清空标签；省略（None）表示不改动标签
    tag_ids: list[int] | None = None


class TransactionOut(BaseModel):
    id: int
    type: str
    direction: str
    occurred_at: datetime
    account_id: int
    account_name: str
    to_account_id: int | None
    to_account_name: str
    category_id: int | None
    category_name: str
    category_icon: str
    category_color: str
    amount_minor: int
    currency: str
    payee: str
    note: str
    status: str
    tags: list[str] = Field(default_factory=list)
    project_id: int | None = None
    project_name: str = ""
    member_id: int | None = None
    member_name: str = ""
    splits: list[SplitOut] = Field(default_factory=list)


class TransactionListOut(BaseModel):
    """分页结果。``total`` 是**满足条件的总数**，不是当页条数。"""

    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


# -----------------------------------------------------------------------------
# 标签 / 项目 / 成员
# -----------------------------------------------------------------------------
class TagCreate(RequestModel):
    name: str = Field(min_length=1, max_length=48)
    color: str = "teal"
    note: str = ""


class TagUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    color: str | None = None
    note: str | None = None


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str
    note: str


class ProjectCreate(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    color: str = "indigo"
    status: Literal["active", "archived"] = "active"
    budget_minor: int = 0
    note: str = ""


class ProjectUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = None
    status: Literal["active", "archived"] | None = None
    budget_minor: int | None = None
    note: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str
    status: str
    budget_minor: int
    note: str


class MemberCreate(RequestModel):
    name: str = Field(min_length=1, max_length=48)
    color: str = "purple"
    is_self: bool = False
    note: str = ""


class MemberUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    color: str | None = None
    is_self: bool | None = None
    note: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str
    is_self: bool
    note: str


# -----------------------------------------------------------------------------
# P2：日历事件 / 卡片墙 / 机构 / 卡面
# -----------------------------------------------------------------------------
class DayEventCreate(RequestModel):
    date: date
    kind: Literal["event", "mood", "anniversary", "note", "todo"] = "event"
    title: str = Field(default="", max_length=96)
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class DayEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date
    kind: str
    title: str
    body: str
    tags: list[Any] = Field(default_factory=list)
    attachments: list[Any] = Field(default_factory=list)
    sort_order: int = 100


class ReorderRequest(RequestModel):
    """拖拽排序：一次提交完整顺序。

    做成"整体提交"而不是逐条 PATCH：拖拽产生的是一个完整的新顺序，
    逐条写入既慢又可能出现中间态（前端刷新时看到半截顺序）。
    """

    order: list[int] = Field(min_length=1, description="账户 id 的目标顺序")


class CardUpdate(RequestModel):
    """卡片外观。字段全部可选，只发改动项。"""

    brand_key: str | None = None
    card_style: str | None = None
    card_network: Literal["", "unionpay", "visa", "mastercard", "amex", "jcb"] | None = None
    theme_tint: str | None = None
    card_no_tail: str | None = Field(default=None, max_length=4)
    sort_order: int | None = None


class InstitutionCreate(RequestModel):
    key: str = Field(min_length=2, max_length=32, description="稳定英文键，如 cmb")
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["bank", "wallet", "broker", "other"] = "bank"
    brand_color: str = "accent"


class InstitutionUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal["bank", "wallet", "broker", "other"] | None = None
    brand_color: str | None = None
    logo_ref: str | None = Field(default=None, max_length=128)
    sort_order: int | None = None


class InstitutionOut(BaseModel):
    id: int
    key: str
    name: str
    kind: str
    brand_color: str
    logo_ref: str
    is_system: bool
    sort_order: int


class CardArtworkCreate(RequestModel):
    key: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=48)
    spec: dict[str, Any]
    kind: Literal["builtin", "uploaded"] = "uploaded"
    file_ref: str = ""


class CardArtworkUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    spec: dict[str, Any] | None = None
    file_ref: str | None = None
    sort_order: int | None = None


class CardArtworkOut(BaseModel):
    id: int
    key: str
    name: str
    kind: str
    spec: dict[str, Any]
    file_ref: str
    #: 用户上传的卡面图片地址；为空表示用 spec 里的自绘渐变
    image_url: str | None = None
    author: str
    license: str
    sort_order: int


# -----------------------------------------------------------------------------
# P1 收尾：周期记账 / 预算 / 债务
# -----------------------------------------------------------------------------
class RecurringRuleCreate(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    type: Literal["expense", "income", "transfer"] = "expense"
    account_id: int
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int = Field(default=0, ge=0)
    currency: str = "CNY"
    payee: str = Field(default="", max_length=64)
    note: str = ""
    frequency: Literal["daily", "weekly", "monthly", "yearly"] = "monthly"
    interval: int = Field(default=1, ge=1, le=120)
    #: 每月第几天；-1 表示月末（写成 31 会让 2 月永远不触发）
    by_month_day: int | None = Field(default=None, ge=-1, le=31)
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    start_date: date
    end_date: date | None = None
    auto_post: bool = False
    lead_days: int = Field(default=3, ge=0, le=90)


class RecurringRuleUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    type: Literal["expense", "income", "transfer"] | None = None
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = None
    payee: str | None = Field(default=None, max_length=64)
    note: str | None = None
    frequency: Literal["daily", "weekly", "monthly", "yearly"] | None = None
    interval: int | None = Field(default=None, ge=1, le=120)
    by_month_day: int | None = Field(default=None, ge=-1, le=31)
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    start_date: date | None = None
    end_date: date | None = None
    auto_post: bool | None = None
    lead_days: int | None = Field(default=None, ge=0, le=90)


class RecurringRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    type: str
    account_id: int
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int
    currency: str
    payee: str
    note: str
    frequency: str
    interval: int
    by_month_day: int | None = None
    by_weekday: int | None = None
    start_date: date
    end_date: date | None = None
    next_due_date: date | None = None
    last_posted_on: date | None = None
    auto_post: bool
    lead_days: int
    generated_count: int


class BudgetCreate(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    scope: Literal["total", "category"] = "total"
    category_id: int | None = None
    period: Literal["weekly", "monthly", "quarterly", "yearly", "custom"] = "monthly"
    amount_minor: int = Field(default=0, ge=0)
    currency: str = "CNY"
    start_date: date | None = None
    end_date: date | None = None
    rollover: bool = False
    alert_threshold: float = Field(default=0.8, gt=0, le=2)
    enabled: bool = True
    note: str = ""


class BudgetUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    scope: Literal["total", "category"] | None = None
    category_id: int | None = None
    period: Literal["weekly", "monthly", "quarterly", "yearly", "custom"] | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    rollover: bool | None = None
    alert_threshold: float | None = Field(default=None, gt=0, le=2)
    enabled: bool | None = None
    note: str | None = None


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    scope: str
    category_id: int | None = None
    period: str
    amount_minor: int
    currency: str
    start_date: date | None = None
    end_date: date | None = None
    rollover: bool
    carryover_minor: int
    alert_threshold: float
    enabled: bool
    note: str


class DebtCreate(RequestModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["lend", "borrow"]
    counterparty: str = Field(default="", max_length=64)
    principal_minor: int = Field(default=0, ge=0)
    currency: str = "CNY"
    account_id: int | None = None
    mirror_account_id: int | None = None
    start_date: date
    due_date: date | None = None
    annual_rate_bps: int = Field(default=0, ge=0, le=100_000)
    #: 还款方式：等额本息 / 等额本金 / 先息后本 / 到期一次性
    repayment_method: Literal["equal_installment", "equal_principal", "interest_first", "lump_sum"] = (
        "lump_sum"
    )
    installments: int = Field(default=1, ge=1, le=600)
    note: str = ""
    #: 是否同时建一个应收/应付账户，让这笔钱进入净值
    create_mirror_account: bool = False


class DebtUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal["lend", "borrow"] | None = None
    counterparty: str | None = Field(default=None, max_length=64)
    principal_minor: int | None = Field(default=None, ge=0)
    currency: str | None = None
    account_id: int | None = None
    mirror_account_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    annual_rate_bps: int | None = Field(default=None, ge=0, le=100_000)
    repayment_method: Literal["equal_installment", "equal_principal", "interest_first", "lump_sum"] | None = (
        None
    )
    installments: int | None = Field(default=None, ge=1, le=600)
    note: str | None = None


class DebtOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: str
    counterparty: str
    principal_minor: int
    currency: str
    account_id: int | None = None
    mirror_account_id: int | None = None
    start_date: date
    due_date: date | None = None
    annual_rate_bps: int
    repayment_method: str
    installments: int
    status: str
    settled_at: date | None = None
    note: str


class DebtPaymentCreate(RequestModel):
    amount_minor: int = Field(gt=0)
    #: 本金与利息都不给时，整笔视为本金 —— 最常见的记账方式，也最保守
    principal_minor: int | None = Field(default=None, ge=0)
    interest_minor: int | None = Field(default=None, ge=0)
    occurred_at: datetime
    tz_offset_minutes: int = 0
    account_id: int | None = None
    note: str = ""


class DebtPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    debt_id: int
    amount_minor: int
    principal_minor: int
    interest_minor: int
    occurred_at: datetime
    tz_offset_minutes: int
    account_id: int | None = None
    transaction_id: int | None = None
    note: str


class SettleRequest(RequestModel):
    status: Literal["active", "settled", "written_off"] = "settled"
    on: date | None = None


# -----------------------------------------------------------------------------
# P1 收尾：记账模板与文本解析
# -----------------------------------------------------------------------------
class TemplateCreate(RequestModel):
    name: str = Field(min_length=1, max_length=48)
    type: Literal["expense", "income", "transfer"] = "expense"
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    #: 为空表示"只填结构，金额每次手输"
    amount_minor: int | None = Field(default=None, ge=0)
    currency: str = "CNY"
    payee: str = Field(default="", max_length=64)
    note: str = ""
    tag_ids: list[int] = Field(default_factory=list)
    project_id: int | None = None
    member_id: int | None = None
    sort_order: int = 0


class TemplateUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    type: Literal["expense", "income", "transfer"] | None = None
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = None
    payee: str | None = Field(default=None, max_length=64)
    note: str | None = None
    tag_ids: list[int] | None = None
    project_id: int | None = None
    member_id: int | None = None
    sort_order: int | None = None


class TemplateOut(BaseModel):
    id: int
    name: str
    type: str
    account_id: int | None = None
    to_account_id: int | None = None
    category_id: int | None = None
    amount_minor: int | None = None
    currency: str
    payee: str
    note: str
    tag_ids: list[int] = Field(default_factory=list)
    project_id: int | None = None
    member_id: int | None = None
    sort_order: int
    usage_count: int
    last_used_at: str | None = None


class ParseTextRequest(RequestModel):
    text: str = Field(min_length=1, max_length=500)


class ParseTextResult(BaseModel):
    """解析结果。``unmatched`` 是契约的一部分：界面必须显示它。"""

    #: 认不出来时给 null，界面让用户自己选 —— 猜错方向会让支出变收入
    type: str | None = None
    amount_minor: int | None = None
    occurred_at: datetime | None = None
    account_id: int | None = None
    category_id: int | None = None
    payee: str = ""
    note: str = ""
    currency: str = "CNY"
    matched: dict[str, Any] = Field(default_factory=dict)
    unmatched: list[str] = Field(default_factory=list)
    raw: str = ""


# -----------------------------------------------------------------------------
# P1 尾巴：回收站与批量操作
# -----------------------------------------------------------------------------
class TrashItemOut(BaseModel):
    entity: str
    id: int
    title: str
    subtitle: str = ""
    deleted_at: str | None = None


class TrashListOut(BaseModel):
    entity: str
    items: list[TrashItemOut]
    total: int


class BatchUpdateRequest(RequestModel):
    """批量修改流水。

    **只应用显式给出的字段**：路由层用 ``model_fields_set`` 判断，
    因此"没传 category_id"（不动）与"传了 category_id=null"（清掉分类）
    是两件事。少了这个区分，批量操作要么改不动、要么误清字段。

    刻意**不含** ``amount_minor`` / ``occurred_at`` / ``type``：
    把一批金额不同的流水改成同一个金额，几乎总是误操作。
    """

    ids: list[int] = Field(min_length=1, max_length=500)
    category_id: int | None = None
    project_id: int | None = None
    member_id: int | None = None
    status: Literal["pending", "cleared", "reconciled", "void"] | None = None
    #: **替换**全部标签
    tag_ids: list[int] | None = None
    #: **追加**标签（批量打标签用），与 tag_ids 互斥
    add_tag_ids: list[int] | None = None


class BatchDeleteRequest(RequestModel):
    ids: list[int] = Field(min_length=1, max_length=500)


# -----------------------------------------------------------------------------
# P1 尾巴：附件
# -----------------------------------------------------------------------------
class AttachmentOut(BaseModel):
    id: int
    kind: str
    transaction_id: int | None = None
    card_artwork_id: int | None = None
    original_name: str
    mime: str
    size_bytes: int
    sha256: str
    created_at: str | None = None
    #: 可直接用于 <img src> 的同源地址
    url: str


# -----------------------------------------------------------------------------
# P4：存钱罐与储蓄目标
# -----------------------------------------------------------------------------
class PiggyBankCreate(RequestModel):
    name: str = Field(min_length=1, max_length=80)
    target_amount_minor: int = Field(gt=0)
    target_name: str = Field(default="", max_length=120)
    currency: str = Field(default="CNY", max_length=8)
    deadline: date | None = None
    kind: Literal["one_time", "long_term", "shared"] = "one_time"
    member_id: int | None = None
    priority: int = Field(default=5, ge=0, le=9)
    skin: str = Field(default="classic", max_length=24)
    hide_amount: bool = False
    goal_id: int | None = None
    note: str = Field(default="", max_length=200)
    #: 建罐时就放一笔进去（"这个罐子里已经有 200 了"），
    #: 免得用户要先建罐、再手动存一次
    initial_minor: int = 0


class PiggyBankUpdate(RequestModel):
    """全部可选。路由用 `exclude_unset=True` 区分"没传"与"传了 null"。"""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    target_name: str | None = Field(default=None, max_length=120)
    target_amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, max_length=8)
    deadline: date | None = None
    kind: Literal["one_time", "long_term", "shared"] | None = None
    status: Literal["active", "achieved", "paused", "abandoned"] | None = None
    member_id: int | None = None
    priority: int | None = Field(default=None, ge=0, le=9)
    skin: str | None = Field(default=None, max_length=24)
    hide_amount: bool | None = None
    goal_id: int | None = None
    note: str | None = Field(default=None, max_length=200)
    sort_order: int | None = None
    celebrated: bool | None = None


class PiggyDepositRequest(RequestModel):
    #: 正=存入，负=取出
    amount_minor: int
    kind: Literal["manual", "auto", "roundup", "change", "milestone", "withdraw", "settle"] = "manual"
    occurred_at: datetime | None = None
    tz_offset_minutes: int = 0
    source_account_id: int | None = None
    transaction_id: int | None = None
    note: str = Field(default="", max_length=200)


class PiggyRuleRequest(RequestModel):
    strategy: Literal[
        "roundup",
        "daily_fixed",
        "weekly_fixed",
        "income_percent",
        "monthly_surplus",
        "category_trigger",
    ]
    enabled: bool = True
    roundup_unit_minor: int = Field(default=100, gt=0)
    fixed_amount_minor: int = Field(default=0, ge=0)
    percent_bps: int = Field(default=0, ge=0, le=10000)
    category_ids: list[int] = Field(default_factory=list)
    account_id: int | None = None
    deduct_from_account: bool = False


class PiggyRuleRunRequest(RequestModel):
    today: date | None = None
    #: 预览而不落库：对自动扣钱的功能来说"先执行再看结果"不可接受
    dry_run: bool = True


class PiggySettleRequest(RequestModel):
    settle: bool = True
    account_id: int | None = None
    occurred_at: datetime | None = None
    create_transaction: bool = True


class PiggyInjectRequest(RequestModel):
    goal_id: int | None = None
    settle_bank: bool = True
    occurred_at: datetime | None = None


class PiggyGoalCreate(RequestModel):
    name: str = Field(min_length=1, max_length=80)
    target_amount_minor: int = Field(gt=0)
    currency: str = Field(default="CNY", max_length=8)
    deadline: date | None = None
    kind: Literal["purchase", "emergency", "travel", "education", "other"] = "purchase"
    account_id: int | None = None
    member_id: int | None = None
    priority: int = Field(default=5, ge=0, le=9)
    hide_amount: bool = False
    note: str = Field(default="", max_length=200)


class PiggyGoalUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    target_amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, max_length=8)
    deadline: date | None = None
    kind: Literal["purchase", "emergency", "travel", "education", "other"] | None = None
    status: Literal["active", "achieved", "paused", "abandoned"] | None = None
    account_id: int | None = None
    member_id: int | None = None
    priority: int | None = Field(default=None, ge=0, le=9)
    hide_amount: bool | None = None
    note: str | None = Field(default=None, max_length=200)
    sort_order: int | None = None
    celebrated: bool | None = None


class PiggyContributeRequest(RequestModel):
    amount_minor: int
    occurred_at: datetime | None = None
    tz_offset_minutes: int = 0
    note: str = Field(default="", max_length=200)


class DepositOut(BaseModel):
    id: int
    piggy_bank_id: int
    amount_minor: int
    occurred_at: str
    tz_offset_minutes: int
    source_account_id: int | None = None
    transaction_id: int | None = None
    kind: str
    note: str


class DepositListOut(BaseModel):
    items: list[DepositOut]
    count: int
    balance_minor: int


class PiggyRuleOut(BaseModel):
    id: int
    piggy_bank_id: int
    strategy: str
    enabled: bool
    roundup_unit_minor: int
    fixed_amount_minor: int
    percent_bps: int
    category_ids: list[int]
    account_id: int | None = None
    deduct_from_account: bool
    last_run_date: str | None = None


class BankOut(BaseModel):
    id: int
    name: str
    target_name: str
    target_amount_minor: int
    balance_minor: int
    currency: str
    deadline: str | None = None
    kind: str
    status: str
    member_id: int | None = None
    priority: int
    skin: str
    hide_amount: bool
    goal_id: int | None = None
    achieved_at: str | None = None
    celebrated: bool
    sort_order: int
    note: str
    ratio: float
    milestones: list[int]
    deleted_at: str | None = None
    rule: PiggyRuleOut | None = None
    #: 双口径预计达成日。字段结构随状态变化（未达成 / 已达成），
    #: 因此用自由字典 —— 强行建模成一个固定形状会让"已达成时没有这些键"
    #: 变成校验错误，而那是最常见的一种正常状态
    eta: dict[str, Any] | None = None
    #: 结清 / 注入后附加的结果字段
    settled_minor: int | None = None
    injected_minor: int | None = None


class BankDetailOut(BankOut):
    deposits: list[DepositOut] = Field(default_factory=list)


class BankListOut(BaseModel):
    items: list[BankOut]
    count: int
    active_target_minor: int
    saved_minor: int


class PiggyActionOut(BaseModel):
    items: list[dict[str, Any]]
    count: int
    total_minor: int
    dry_run: bool


class ContributionOut(BaseModel):
    id: int
    goal_id: int
    amount_minor: int
    occurred_at: str
    tz_offset_minutes: int
    note: str


class GoalOut(BaseModel):
    id: int
    name: str
    target_amount_minor: int
    saved_minor: int
    currency: str
    deadline: str | None = None
    kind: str
    status: str
    account_id: int | None = None
    member_id: int | None = None
    priority: int
    hide_amount: bool
    achieved_at: str | None = None
    celebrated: bool
    sort_order: int
    note: str
    ratio: float
    milestones: list[int]
    deleted_at: str | None = None


class GoalDetailOut(GoalOut):
    contributions: list[ContributionOut] = Field(default_factory=list)


class GoalListOut(BaseModel):
    items: list[GoalOut]
    count: int
    active_target_minor: int
    saved_minor: int


class GoalContributionsOut(BaseModel):
    items: list[ContributionOut]
    count: int
    saved_minor: int


# -----------------------------------------------------------------------------
# P5：台账与报表
# -----------------------------------------------------------------------------
class LedgerEntryOut(BaseModel):
    transaction_id: int
    occurred_at: str
    tz_offset_minutes: int
    type: str
    direction: str
    amount_minor: int
    signed_minor: int
    running_balance_minor: int
    payee: str
    note: str
    status: str
    source: str
    category_id: int | None = None
    category_name: str
    category_kind: str
    to_account_id: int | None = None
    #: 转账时是对方账户名；非转账为空
    counterparty: str
    #: 'self' = 本账户是转出方；'incoming' = 本账户收到了这笔转账
    leg: str
    project_id: int | None = None
    member_id: int | None = None
    has_splits: bool


class LedgerCheckOut(BaseModel):
    internal_ok: bool
    aggregate_ok: bool
    covers_today: bool
    opening_balance_minor: int
    closing_balance_minor: int
    recomputed_closing_minor: int
    #: 截至区间末的余额（台账期末应当等于它）
    aggregate_balance_minor: int
    #: 全时段余额。与上面不同说明有未来日期的流水
    all_time_balance_minor: int
    difference_minor: int
    excluded_void_count: int
    future_dated_count: int
    future_dated_net_minor: int
    balanced: bool


class LedgerOut(BaseModel):
    account_id: int
    account_name: str
    currency: str
    start: str
    end: str
    opening_balance_minor: int
    closing_balance_minor: int
    entries: list[LedgerEntryOut]
    count: int
    inflow_minor: int
    outflow_minor: int
    check: LedgerCheckOut


class TrialBalanceOut(BaseModel):
    account_count: int
    balance_change_minor: int
    income_minor: int
    expense_minor: int
    adjust_net_minor: int
    expected_change_minor: int
    transfer_neutral_ok: bool
    broken_transfer_ids: list[int]
    balanced: bool


class ReconcileRequest(RequestModel):
    actual_balance_minor: int
    as_of: date | None = None
    note: str = Field(default="", max_length=200)
    #: 为假时只算差异、不落库（界面需要能先给用户看）
    create_adjustment: bool = True


class ReconcileOut(BaseModel):
    account_id: int
    account_name: str
    as_of: str
    computed_balance_minor: int
    actual_balance_minor: int
    difference_minor: int
    created_transaction_id: int | None = None
    new_balance_minor: int


class ReportOut(BaseModel):
    """报告文档。

    这里**刻意不把 sections / insights 建成精确模型**：
    块（block）是报告的唯一扩展点，新增一种展示只需要加一个块类型。
    把每种块都建成 Pydantic 模型会让"加一个块类型"变成"改三处"
    （schema、导出、渲染器），而那正是这个设计想避免的。
    顶层字段仍然精确 —— 它们是稳定契约，块不是。
    """

    schema_: str = Field(alias="schema")
    kind: str
    title: str
    subtitle: str
    period: dict[str, Any]
    generated_at: str
    currency: str
    cover: dict[str, Any]
    kpis: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    insights: list[dict[str, Any]]
    notes: list[str]

    model_config = ConfigDict(populate_by_name=True)


# -----------------------------------------------------------------------------
# P5：AI 分析
# -----------------------------------------------------------------------------
class AiConfigOut(BaseModel):
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float
    redact: bool
    #: **只给"填过没有"，不回传密钥本身**
    has_key: bool


class AiConfigUpdate(RequestModel):
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=64)
    timeout_seconds: float | None = None
    redact: bool | None = None
    #: 三态：不传=保持，空串=清除，有值=替换
    api_key: str | None = Field(default=None, max_length=300)


class AiAnalysisOut(BaseModel):
    id: int
    report_kind: str
    period_start: str
    period_end: str
    source: str
    model: str
    redacted: bool
    content: str
    fallback_reason: str
    error: str
    created_at: str | None = None


class AiAnalysisListOut(BaseModel):
    items: list[AiAnalysisOut]
    count: int


# -----------------------------------------------------------------------------
# 元数据
# -----------------------------------------------------------------------------
class CurrencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    symbol: str
    minor_units: int


class EnumsOut(BaseModel):
    """把领域枚举下发给前端，避免前端再手抄一份选项列表。"""

    account_types: list[str]
    category_kinds: list[str]
    transaction_types: list[str]
    transaction_statuses: list[str]
    transaction_sources: list[str]
    directions: list[str]
    project_statuses: list[str]
