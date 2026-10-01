"""金额与领域基础用例。

这是全项目**最该有测试**的地方：金额算错是记账应用唯一无法辩解的错误。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from accountbook.core.domain import TRANSFER_TYPES, CategoryKind, TransactionType
from accountbook.core.money import (
    MoneyError,
    currency_symbol,
    from_minor,
    minor_units,
    parse_amount,
    quantize,
    to_minor,
)


class TestToMinor:
    """「元」→ 最小单位。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("0"), 0),
            (Decimal("1"), 100),
            (Decimal("12.34"), 1234),
            (Decimal("0.01"), 1),
            (Decimal("1234.56"), 123456),
            (Decimal("-9.99"), -999),
            (0, 0),
            (1, 100),
            ("12.34", 1234),
            ("  12.34  ", 1234),
        ],
    )
    def test_basic_conversions(self, value: object, expected: int) -> None:
        assert to_minor(value) == expected  # type: ignore[arg-type]

    def test_rejects_float(self) -> None:
        """浮点必须被直接拒绝，而不是"看起来能用但偶尔差一分"。"""
        with pytest.raises(MoneyError, match="浮点"):
            to_minor(12.34)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        with pytest.raises(MoneyError, match="布尔"):
            to_minor(True)  # type: ignore[arg-type]

    def test_rejects_garbage(self) -> None:
        with pytest.raises(MoneyError):
            to_minor("十二块")

    def test_rejects_empty(self) -> None:
        with pytest.raises(MoneyError, match="为空"):
            to_minor("   ")

    def test_rounds_half_up_at_currency_precision(self) -> None:
        """第三位小数按四舍五入处理，且结果必须是整数。"""
        assert to_minor(Decimal("1.005")) == 101  # 1.005 → 1.01（ROUND_HALF_UP）
        assert to_minor(Decimal("1.004")) == 100

    def test_zero_decimal_currency(self) -> None:
        """日元没有小数位：100 JPY 就是 100，而不是 10000。"""
        assert to_minor(Decimal("100"), "JPY") == 100
        assert to_minor(Decimal("100.4"), "JPY") == 100
        assert to_minor(Decimal("100.6"), "JPY") == 101

    def test_unknown_currency_falls_back_to_two_digits(self) -> None:
        """未知币种按常规处理，而不是让整个账本打不开。"""
        assert to_minor(Decimal("1.23"), "XYZ") == 123
        assert minor_units("XYZ") == 2


class TestFromMinor:
    """最小单位 → 「元」。"""

    def test_round_trip_is_lossless(self) -> None:
        for raw in ("0", "0.01", "12.34", "999999.99", "-12.34"):
            assert from_minor(to_minor(Decimal(raw))) == quantize(Decimal(raw))

    def test_rejects_non_integer(self) -> None:
        with pytest.raises(MoneyError, match="整数"):
            from_minor(12.5)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        with pytest.raises(MoneyError, match="整数"):
            from_minor(True)  # type: ignore[arg-type]


class TestParseAmount:
    """用户输入解析 —— 快捷记账场景下的容错是刚需。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("35", 3500),
            ("35.5", 3550),
            ("¥35", 3500),
            ("￥35.00", 3500),
            ("$35", 3500),
            ("1234.5元", 123450),
            ("¥1,234.56", 123456),
            ("1，234.56", 123456),
            ("35 元", 3500),
            ("－35", -3500),
            ("３５．５", 3550),  # 全角数字（中文输入法下常见）
        ],
    )
    def test_tolerates_common_handwritten_forms(self, text: str, expected: int) -> None:
        assert parse_amount(text) == expected

    def test_rejects_non_string(self) -> None:
        with pytest.raises(MoneyError, match="字符串"):
            parse_amount(35)  # type: ignore[arg-type]

    def test_rejects_symbol_only(self) -> None:
        with pytest.raises(MoneyError, match="为空"):
            parse_amount("¥")


class TestCurrencyHelpers:
    def test_symbol_lookup(self) -> None:
        assert currency_symbol("CNY") == "¥"
        assert currency_symbol("USD") == "$"
        assert currency_symbol("cnY") == "¥", "大小写不应影响识别"

    def test_unknown_currency_returns_code(self) -> None:
        """回退为代码本身，便于一眼看出配置异常。"""
        assert currency_symbol("XYZ") == "XYZ"


class TestDomainEnums:
    def test_transfer_types_excluded_from_income_expense_stats(self) -> None:
        """转账与校准不得计入收支统计 —— 这是口径错误最常见的来源。"""
        assert TransactionType.TRANSFER in TRANSFER_TYPES
        assert TransactionType.ADJUST in TRANSFER_TYPES
        assert TransactionType.EXPENSE not in TRANSFER_TYPES
        assert TransactionType.INCOME not in TRANSFER_TYPES

    def test_category_kinds_cover_both_directions(self) -> None:
        assert {kind.value for kind in CategoryKind} == {"expense", "income", "transfer"}
