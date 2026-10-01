"""金额运算 —— 全项目**唯一**允许处理金额精度的地方。

铁律
----
1. 金额在数据库、API、领域对象中一律是**整数最小单位**（人民币即"分"）。
   浮点数绝不参与金额运算 —— ``0.1 + 0.2`` 的教训不必再交一次学费。
2. 只有在**解析用户输入**与**格式化给用户看**这两个边界上，才出现 ``Decimal``。
   即便在边界上也不用 ``float``：``Decimal("12.34")`` 是精确的，
   ``float("12.34")`` 不是。
3. 币种的最小单位位数不同（日元/韩元为 0 位），因此所有换算都必须传入币种，
   不允许出现"默认两位"的隐式假设。

需求追溯：REQ-6（记账核心）、REQ-13（金额展示）
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = [
    "DEFAULT_CURRENCY",
    "SUPPORTED_CURRENCIES",
    "MoneyError",
    "from_minor",
    "minor_units",
    "parse_amount",
    "quantize",
    "to_minor",
]

#: 默认币种（中文用户）
DEFAULT_CURRENCY = "CNY"

#: 支持的最小单位位数。新增币种只需在此登记，全链路自动适配。
#: 日元 / 韩元没有小数位 —— 若强行按两位处理，所有金额都会差 100 倍。
_CURRENCY_MINOR_UNITS: dict[str, int] = {
    "CNY": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "TWD": 2,
    "SGD": 2,
    "AUD": 2,
    "CAD": 2,
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
}

#: 币种符号（仅用于展示；真实格式化在前端完成，这里供后端生成文本报表使用）
_CURRENCY_SYMBOLS: dict[str, str] = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "HKD": "HK$",
    "TWD": "NT$",
    "SGD": "S$",
    "AUD": "A$",
    "CAD": "C$",
    "JPY": "¥",
    "KRW": "₩",
    "VND": "₫",
}

SUPPORTED_CURRENCIES: tuple[str, ...] = tuple(_CURRENCY_MINOR_UNITS)


class MoneyError(ValueError):
    """金额非法（格式错误、精度超出币种、数值越界）。"""


def minor_units(currency: str = DEFAULT_CURRENCY) -> int:
    """返回币种的最小单位位数。

    未知币种按 2 位处理而不是报错：宁可把没见过的新币种当常规货币处理，
    也不要因为用户手改了配置里的币种代码就让整个账本打不开。
    """
    return _CURRENCY_MINOR_UNITS.get((currency or DEFAULT_CURRENCY).upper(), 2)


def currency_symbol(currency: str = DEFAULT_CURRENCY) -> str:
    """返回币种符号；未知币种回退为币种代码本身（便于一眼看出异常）。"""
    code = (currency or DEFAULT_CURRENCY).upper()
    return _CURRENCY_SYMBOLS.get(code, code)


def quantize(value: Decimal, currency: str = DEFAULT_CURRENCY) -> Decimal:
    """把 Decimal 按币种精度四舍五入（采用金融常用的 ROUND_HALF_UP）。"""
    digits = minor_units(currency)
    exponent = Decimal(1).scaleb(-digits)  # 2 位 → Decimal("0.01")
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def to_minor(value: Decimal | int | str, currency: str = DEFAULT_CURRENCY) -> int:
    """把「元」转换成整数最小单位（分）。

    接受 ``Decimal`` / ``int`` / ``str``，**不接受 float** ——
    传入 float 会直接抛错，而不是"看起来能用但结果偶尔差一分"。
    这种错误必须在开发期被拦住。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，单独挡掉
        raise MoneyError("金额不能是布尔值")
    if isinstance(value, float):
        raise MoneyError("金额不接受浮点数，请使用 Decimal 或字符串（例如 Decimal('12.34')）")

    if isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "")
        if not text:
            raise MoneyError("金额为空")
        try:
            decimal_value = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"无法解析金额：{value!r}") from exc
    else:
        raise MoneyError(f"不支持的金额类型：{type(value).__name__}")

    if not decimal_value.is_finite():
        raise MoneyError("金额必须是有限数值")

    scaled = quantize(decimal_value, currency) * (10 ** minor_units(currency))
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str = DEFAULT_CURRENCY) -> Decimal:
    """把整数最小单位还原为 Decimal「元」值（用于报表文本与导出）。"""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise MoneyError(f"最小单位金额必须是整数，收到 {type(minor).__name__}")
    return quantize(Decimal(minor) / (10 ** minor_units(currency)), currency)


def parse_amount(text: str, currency: str = DEFAULT_CURRENCY) -> int:
    """解析用户输入的金额文本为最小单位。

    容忍常见的手写形式：带币种符号、千分位、全角字符、前后空格、中文"元"。
    这类容错是必要的 —— 快捷记账场景下用户会直接粘贴
    ``¥1,234.56`` 或 ``1234.5元``，为此报错会毁掉"快速"二字。
    """
    if not isinstance(text, str):
        raise MoneyError("金额文本必须是字符串")

    cleaned = text.strip()
    for noise in ("¥", "￥", "$", "€", "£", "元", "人民币", "RMB", "rmb", " ", "\u3000"):
        cleaned = cleaned.replace(noise, "")
    cleaned = cleaned.replace(",", "").replace("，", "")
    if not cleaned:
        raise MoneyError("金额为空")

    # 全角数字与小数点归一化（中文输入法下极易发生）
    cleaned = cleaned.translate(str.maketrans("０１２３４５６７８９．－", "0123456789.-"))
    return to_minor(cleaned, currency)
