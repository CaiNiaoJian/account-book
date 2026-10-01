"""领域枚举与值对象 —— 与存储、界面都无关的纯业务词汇。

为什么把枚举集中在这里
----------------------
这些取值同时出现在四处：数据库列、API 契约、前端类型、界面文案。
如果各写一份字符串字面量，改一个值就要全仓库搜索替换，且极易漏。
集中定义后：

* 数据库用 ``String`` 列存枚举值（而不是 SQLite 不支持的 ENUM），
  但取值必须来自这里；
* API 通过 ``/api/meta/enums`` 把同一份定义下发给前端；
* 前端据此生成选项，不需要手抄一份列表。

需求追溯：REQ-3（功能齐全）、REQ-4（分类精细）、REQ-6（记账核心）
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "TRANSFER_TYPES",
    "AccountType",
    "CategoryKind",
    "EntryState",
    "ProjectStatus",
    "TransactionSource",
    "TransactionStatus",
    "TransactionType",
]


class AccountType(StrEnum):
    """账户类型。

    划分依据是"这笔钱以什么形态存在、影响哪些统计口径"，
    而不是具体的银行名称 —— 后者属于机构字段。
    """

    CASH = "cash"  # 现金
    DEBIT_CARD = "debit_card"  # 储蓄卡
    CREDIT_CARD = "credit_card"  # 信用卡（负债：余额为负表示欠款）
    E_WALLET = "e_wallet"  # 电子钱包（微信 / 支付宝 / 云闪付）
    INVESTMENT = "investment"  # 投资（基金 / 股票 / 理财）
    RECEIVABLE = "receivable"  # 应收（借出去的钱）
    PAYABLE = "payable"  # 应付（欠别人的钱）
    PREPAID = "prepaid"  # 预付卡 / 储值卡（公交卡、饭卡）
    VIRTUAL = "virtual"  # 虚拟账户（预算池、待分配）


class CategoryKind(StrEnum):
    """分类适用的流水方向。

    刻意允许分类带方向：把"工资"放进支出分类是明显的记账错误，
    让数据模型从一开始就阻止它，比事后靠用户自觉可靠。
    """

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"


class TransactionType(StrEnum):
    """流水类型。"""

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"  # 账户间转账，不计入收支统计
    ADJUST = "adjust"  # 余额校准（对账差额），不计入收支统计


class TransactionStatus(StrEnum):
    """流水状态机（REQ-5 台账与对账的基础）。"""

    PENDING = "pending"  # 待清算（例如信用卡消费尚未入账）
    CLEARED = "cleared"  # 已清算
    RECONCILED = "reconciled"  # 已对账
    VOID = "void"  # 作废（保留记录但排除统计）


class TransactionSource(StrEnum):
    """流水来源 —— 排查"这笔是哪来的"时唯一可靠的依据。"""

    MANUAL = "manual"
    QUICK_ADD = "quick_add"
    IMPORT = "import"
    RECURRING = "recurring"  # 周期记账自动生成
    PLUGIN = "plugin"
    API = "api"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class EntryState(StrEnum):
    """某一天的登记状态（REQ-20 日历颜色指标之一）。"""

    NONE = "none"  # 未登记
    PARTIAL = "partial"  # 部分登记
    COMPLETE = "complete"  # 已完成


#: 不计入收支统计的流水类型集合。
#: 转账与余额校准会让"收入/支出"虚高 —— 这是记账软件最常见的口径错误来源，
#: 因此集中声明，所有统计口径都必须引用它。
TRANSFER_TYPES: frozenset[TransactionType] = frozenset({TransactionType.TRANSFER, TransactionType.ADJUST})
