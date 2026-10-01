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

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AccountCreate",
    "AccountOut",
    "AccountOverviewOut",
    "AccountUpdate",
    "CategoryCreate",
    "CategoryMergeResult",
    "CategoryMoveRequest",
    "CategoryNode",
    "CategoryOut",
    "CategoryUpdate",
    "CurrencyOut",
    "EnumsOut",
    "MemberCreate",
    "MemberOut",
    "MemberUpdate",
    "ProjectCreate",
    "ProjectOut",
    "ProjectUpdate",
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
