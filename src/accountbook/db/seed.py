"""内置种子数据 —— 让用户第一次打开就有可用的分类与账户。

设计立场
--------
一个记账应用如果打开后是"零分类、零账户"，用户要先把账本"装修"一遍才能记账，
这件事会劝退大多数人。因此首次启动写入：

* **币种字典**（常用 12 种，含最小单位位数）；
* **精细分类树**（支出 14 大类 / 收入 5 大类，共 100+ 节点，全部可改可隐藏）；
* **三个默认账户**（现金 / 微信 / 支付宝）—— 覆盖国内最常见的支付方式。

分类全部标记 ``is_system=True``：**可改名、可改图标、可隐藏，但不可删除**。
理由见 :class:`~accountbook.core.errors.ProtectedEntityError` 的说明。

幂等与升级
----------
用 ``app_settings.seed_version`` 记录已初始化到哪一版。
每次启动都会检查：版本落后则补齐新增的种子项（例如后续版本新增了分类），
已经存在的项**不会被覆盖** —— 用户改过的名字与图标必须保留。

需求追溯：REQ-3（内容齐全）、REQ-4（分类精细、图标优雅）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.domain import AccountType, CategoryKind
from ..core.money import DEFAULT_CURRENCY
from .models import Account, AppSetting, Category, Currency

__all__ = ["SEED_CATEGORY_TREE", "SEED_VERSION", "ensure_seed_data", "seed_currencies"]

_logger = logging.getLogger(__name__)

#: 种子数据版本。**新增内置分类或币种时递增它**，老用户下次启动即可补齐。
SEED_VERSION = 1

_SEED_VERSION_KEY = "seed_version"


# -----------------------------------------------------------------------------
# 分类树定义
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SeedCategory:
    """一个种子分类节点。

    ``icon`` 取值必须存在于前端图标集（``web/src/components/Icon.tsx``）；
    写错不会报错，只会显示为默认图标 —— 因此新增分类时请对照图标清单。
    """

    name: str
    icon: str
    color: str
    children: tuple[SeedCategory, ...] = field(default=())


def _leaf(name: str, icon: str, color: str) -> SeedCategory:
    return SeedCategory(name=name, icon=icon, color=color)


#: 支出分类树。分类粒度以"能回答'钱花在哪了'且不至于记不过来"为准。
EXPENSE_TREE: tuple[SeedCategory, ...] = (
    SeedCategory(
        "餐饮",
        "food",
        "orange",
        (
            _leaf("早餐", "food", "orange"),
            _leaf("午餐", "food", "orange"),
            _leaf("晚餐", "food", "orange"),
            _leaf("零食", "snack", "yellow"),
            _leaf("饮品咖啡", "coffee", "brown"),
            _leaf("外卖", "takeout", "orange"),
            _leaf("聚餐请客", "dining", "pink"),
            _leaf("食材买菜", "groceries", "green"),
        ),
    ),
    SeedCategory(
        "交通",
        "transport",
        "teal",
        (
            _leaf("公交地铁", "bus", "teal"),
            _leaf("打车", "taxi", "teal"),
            _leaf("加油", "fuel", "orange"),
            _leaf("停车费", "parking", "teal"),
            _leaf("过路费", "toll", "teal"),
            _leaf("火车高铁", "train", "indigo"),
            _leaf("飞机", "plane", "indigo"),
            _leaf("共享单车", "bike", "green"),
            _leaf("车辆保养", "car", "teal"),
        ),
    ),
    SeedCategory(
        "居住",
        "home",
        "indigo",
        (
            _leaf("房租", "home", "indigo"),
            _leaf("房贷", "bank", "indigo"),
            _leaf("物业费", "building", "indigo"),
            _leaf("水费", "water", "info"),
            _leaf("电费", "bolt", "yellow"),
            _leaf("燃气费", "flame", "orange"),
            _leaf("取暖费", "heater", "orange"),
            _leaf("宽带网费", "wifi", "info"),
            _leaf("家电家具", "sofa", "brown"),
            _leaf("维修", "tools", "gray"),
        ),
    ),
    SeedCategory(
        "购物",
        "shopping",
        "pink",
        (
            _leaf("服饰鞋包", "shirt", "pink"),
            _leaf("日用百货", "basket", "pink"),
            _leaf("美妆护肤", "cosmetics", "pink"),
            _leaf("数码电器", "device", "indigo"),
            _leaf("家居用品", "sofa", "brown"),
            _leaf("母婴用品", "baby", "pink"),
        ),
    ),
    SeedCategory(
        "医疗健康",
        "health",
        "red",
        (
            _leaf("门诊", "hospital", "red"),
            _leaf("药品", "pill", "red"),
            _leaf("体检", "stethoscope", "red"),
            _leaf("住院", "hospital", "red"),
            _leaf("牙科", "tooth", "red"),
            _leaf("保健品", "pill", "green"),
        ),
    ),
    SeedCategory(
        "教育",
        "education",
        "blue",
        (
            _leaf("书籍", "book", "blue"),
            _leaf("课程培训", "graduation", "blue"),
            _leaf("考试报名", "exam", "blue"),
            _leaf("学费", "graduation", "indigo"),
            _leaf("文具", "pen", "blue"),
        ),
    ),
    SeedCategory(
        "娱乐",
        "leisure",
        "purple",
        (
            _leaf("电影演出", "film", "purple"),
            _leaf("游戏", "game", "purple"),
            _leaf("旅行", "travel", "teal"),
            _leaf("运动健身", "dumbbell", "green"),
            _leaf("会员订阅", "repeat", "purple"),
            _leaf("书报杂志", "book", "blue"),
        ),
    ),
    SeedCategory(
        "人情往来",
        "social",
        "pink",
        (
            _leaf("红包", "gift", "red"),
            _leaf("礼品", "gift", "pink"),
            _leaf("请客", "dining", "pink"),
            _leaf("捐赠", "heart", "red"),
            _leaf("孝敬长辈", "heart", "pink"),
        ),
    ),
    SeedCategory(
        "通讯",
        "communication",
        "info",
        (
            _leaf("手机话费", "phone", "info"),
            _leaf("流量充值", "signal", "info"),
        ),
    ),
    SeedCategory(
        "宠物",
        "pet",
        "brown",
        (
            _leaf("宠物食品", "bone", "brown"),
            _leaf("宠物医疗", "vet", "brown"),
            _leaf("宠物用品", "pet", "brown"),
        ),
    ),
    SeedCategory(
        "育儿",
        "childcare",
        "pink",
        (
            _leaf("奶粉尿布", "baby", "pink"),
            _leaf("托育", "childcare", "pink"),
            _leaf("玩具", "toy", "yellow"),
            _leaf("儿童教育", "education", "blue"),
        ),
    ),
    SeedCategory(
        "金融与税费",
        "finance",
        "gray",
        (
            _leaf("手续费", "percent", "gray"),
            _leaf("利息支出", "interest", "gray"),
            _leaf("保险", "shield", "indigo"),
            _leaf("税费", "receipt", "gray"),
            _leaf("罚款", "alert", "red"),
        ),
    ),
    SeedCategory(
        "工作",
        "work",
        "indigo",
        (
            _leaf("办公用品", "briefcase", "indigo"),
            _leaf("差旅", "travel", "indigo"),
        ),
    ),
    SeedCategory(
        "其他支出",
        "other",
        "gray",
        (_leaf("未分类支出", "tag", "gray"),),
    ),
)

#: 收入分类树。刻意比支出浅一层 —— 收入来源本来就少，
#: 过度细分会让人在记账时反复犹豫"这算哪一类"。
INCOME_TREE: tuple[SeedCategory, ...] = (
    SeedCategory(
        "职业收入",
        "salary",
        "green",
        (
            _leaf("工资", "salary", "green"),
            _leaf("奖金", "award", "green"),
            _leaf("加班费", "overtime", "green"),
            _leaf("提成", "percent", "green"),
            _leaf("年终奖", "award", "yellow"),
            _leaf("补贴", "subsidy", "green"),
        ),
    ),
    SeedCategory(
        "经营与副业",
        "business",
        "teal",
        (
            _leaf("兼职", "parttime", "teal"),
            _leaf("稿费", "pen", "teal"),
            _leaf("经营收入", "shop", "teal"),
        ),
    ),
    SeedCategory(
        "投资收入",
        "investment",
        "indigo",
        (
            _leaf("利息收入", "interest", "indigo"),
            _leaf("理财收益", "chart", "indigo"),
            _leaf("股票基金", "kline", "indigo"),
            _leaf("分红", "dividend", "indigo"),
        ),
    ),
    SeedCategory(
        "转移性收入",
        "transfer_in",
        "info",
        (
            _leaf("报销", "receipt", "info"),
            _leaf("红包收入", "gift", "red"),
            _leaf("退款", "refund", "info"),
            _leaf("礼金", "gift", "pink"),
        ),
    ),
    SeedCategory(
        "其他收入",
        "other",
        "gray",
        (_leaf("未分类收入", "tag", "gray"),),
    ),
)

#: 分类树总表（供测试与文档引用）
SEED_CATEGORY_TREE: dict[str, tuple[SeedCategory, ...]] = {
    CategoryKind.EXPENSE.value: EXPENSE_TREE,
    CategoryKind.INCOME.value: INCOME_TREE,
}


# -----------------------------------------------------------------------------
# 币种
# -----------------------------------------------------------------------------
#: (代码, 名称, 符号, 最小单位位数)
SEED_CURRENCIES: tuple[tuple[str, str, str, int], ...] = (
    ("CNY", "人民币", "¥", 2),
    ("USD", "美元", "$", 2),
    ("EUR", "欧元", "€", 2),
    ("GBP", "英镑", "£", 2),
    ("HKD", "港币", "HK$", 2),
    ("TWD", "新台币", "NT$", 2),
    ("SGD", "新加坡元", "S$", 2),
    ("AUD", "澳元", "A$", 2),
    ("CAD", "加元", "C$", 2),
    ("JPY", "日元", "¥", 0),
    ("KRW", "韩元", "₩", 0),
    ("VND", "越南盾", "₫", 0),
)

#: 默认账户：(名称, 类型, 图标, 颜色, 排序)
SEED_ACCOUNTS: tuple[tuple[str, str, str, str, int], ...] = (
    ("现金", AccountType.CASH.value, "cash", "green", 10),
    ("微信", AccountType.E_WALLET.value, "wechat", "green", 20),
    ("支付宝", AccountType.E_WALLET.value, "alipay", "blue", 30),
)


# -----------------------------------------------------------------------------
# 写入
# -----------------------------------------------------------------------------
def seed_currencies(session: Session) -> int:
    """补齐缺失的币种，返回新增数量。已存在的不覆盖（用户可能改过显示名）。"""
    existing = set(session.scalars(select(Currency.code)).all())
    added = 0
    for index, (code, name, symbol, minor_units) in enumerate(SEED_CURRENCIES):
        if code in existing:
            continue
        session.add(
            Currency(
                code=code,
                name=name,
                symbol=symbol,
                minor_units=minor_units,
                sort_order=index * 10,
            )
        )
        added += 1
    return added


def _insert_tree(
    session: Session,
    tree: tuple[SeedCategory, ...],
    kind: str,
    *,
    parent: Category | None = None,
    existing_names: dict[tuple[int | None, str], Category],
) -> int:
    """递归写入一棵分类树，返回新增数量。

    ``existing_names`` 以 ``(parent_id, name)`` 为键做幂等判断 ——
    用名字而不是 id，因为种子数据里的 id 在用户库里并不固定。
    同名分类若已存在（无论是否用户自建），一律跳过，绝不覆盖。
    """
    added = 0
    for order, node in enumerate(tree):
        key = (parent.id if parent else None, node.name)
        current = existing_names.get(key)
        if current is None:
            current = Category(
                name=node.name,
                kind=kind,
                parent_id=parent.id if parent else None,
                depth=(parent.depth + 1) if parent else 0,
                icon=node.icon,
                color=node.color,
                is_system=True,
                sort_order=order * 10,
            )
            session.add(current)
            # flush 以获得自增 id，供子节点与物化路径使用
            session.flush()
            current.path = f"{parent.path}/{current.id}" if parent else f"/{current.id}"
            existing_names[key] = current
            added += 1

        if node.children:
            added += _insert_tree(
                session,
                node.children,
                kind,
                parent=current,
                existing_names=existing_names,
            )
    return added


def seed_categories(session: Session) -> int:
    """补齐缺失的内置分类（含子分类），返回新增数量。"""
    rows = session.scalars(select(Category)).all()
    existing: dict[tuple[int | None, str], Category] = {(row.parent_id, row.name): row for row in rows}

    added = 0
    for kind, tree in SEED_CATEGORY_TREE.items():
        added += _insert_tree(session, tree, kind, existing_names=existing)
    return added


def seed_accounts(session: Session) -> int:
    """首次建库时写入默认账户。

    只在**一个账户都没有**时执行：用户可能已经删掉了默认账户（软删除），
    若无脑补齐，会让人产生"删不掉"的错觉。
    """
    has_any = session.scalar(select(Account.id).limit(1)) is not None
    if has_any:
        return 0

    for name, account_type, icon, color, order in SEED_ACCOUNTS:
        session.add(
            Account(
                name=name,
                type=account_type,
                icon=icon,
                color=color,
                sort_order=order,
                currency=DEFAULT_CURRENCY,
            )
        )
    return len(SEED_ACCOUNTS)


def ensure_seed_data(session: Session) -> dict[str, int]:
    """确保种子数据就绪；返回本次新增数量（供日志与测试断言）。

    幂等：重复调用不会重复写入，也不会覆盖用户对内置条目的修改。
    """
    setting = session.get(AppSetting, _SEED_VERSION_KEY)
    applied_version = 0
    if setting is not None:
        raw = setting.value.get("version")
        applied_version = int(raw) if isinstance(raw, (int, float, str)) and str(raw).isdigit() else 0

    if applied_version >= SEED_VERSION:
        return {"currencies": 0, "categories": 0, "accounts": 0}

    counts = {
        "currencies": seed_currencies(session),
        "categories": seed_categories(session),
        "accounts": seed_accounts(session),
    }

    if setting is None:
        setting = AppSetting(key=_SEED_VERSION_KEY, value={"version": SEED_VERSION})
        session.add(setting)
    else:
        setting.value = {"version": SEED_VERSION}

    _logger.info(
        "内置数据已初始化（seed_version=%s）：币种 +%s，分类 +%s，账户 +%s",
        SEED_VERSION,
        counts["currencies"],
        counts["categories"],
        counts["accounts"],
    )
    return counts
