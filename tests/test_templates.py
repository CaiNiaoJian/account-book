"""记账模板与文本解析用例（P1 收尾）。

解析器的核心承诺是**绝不猜**，因此用例的重点不是"能认出多少"，
而是"认不出时是否老实地把词交还给用户"。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from accountbook.core.errors import NotFoundError, ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import categories as categories_service
from accountbook.services import templates as templates_service

TODAY = date(2026, 10, 1)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ledger.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database):
    with db.session() as active:
        yield active


def _category(session: Session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    raise AssertionError(f"未找到分类 {name}")


class TestTemplates:
    def _template(self, session: Session, **overrides):
        payload = {
            "name": "午餐",
            "type": "expense",
            "account_id": accounts_service.list_accounts(session)[0].id,
            "category_id": _category(session, "午餐"),
            "amount_minor": 3_800,
            "payee": "公司食堂",
        }
        payload.update(overrides)
        return templates_service.create_template(session, **payload)

    def test_create_and_serialize(self, session: Session) -> None:
        template = self._template(session, tag_ids=[])
        payload = templates_service.serialize(template)
        assert payload["name"] == "午餐"
        assert payload["amount_minor"] == 3_800
        assert payload["usage_count"] == 0
        assert payload["tag_ids"] == []

    def test_amount_may_be_empty(self, session: Session) -> None:
        """金额可以为空：有些模板只填结构（"打车"，金额每次不同）。"""
        template = self._template(session, name="打车", amount_minor=None)
        assert templates_service.serialize(template)["amount_minor"] is None

    def test_unknown_reference_rejected(self, session: Session) -> None:
        with pytest.raises(NotFoundError):
            self._template(session, account_id=999_999)
        with pytest.raises(NotFoundError):
            self._template(session, category_id=999_999)

    def test_transfer_template_needs_target(self, session: Session) -> None:
        with pytest.raises(ValidationError, match="目标账户"):
            self._template(session, type="transfer", to_account_id=None)

    def test_sorted_by_usage_then_recency(self, session: Session) -> None:
        """按"最常用"排序：按创建时间排会让第三个月建的模板永远排最后，
        而用户最想点的恰恰是天天用的那几个。
        """
        first = self._template(session, name="A")
        second = self._template(session, name="B")
        third = self._template(session, name="C")

        templates_service.apply_template(session, third.id)
        templates_service.apply_template(session, third.id)
        templates_service.apply_template(session, second.id)

        order = [item.name for item in templates_service.list_templates(session)]
        assert order == ["C", "B", "A"], f"排序应为最常用优先：{order}"
        session.refresh(first)
        assert first.usage_count == 0

    def test_apply_records_usage(self, session: Session) -> None:
        template = self._template(session)
        draft = templates_service.apply_template(session, template.id)
        assert draft["usage_count"] == 1
        assert draft["last_used_at"] is not None
        session.refresh(template)
        assert template.last_used_at is not None

    def test_apply_does_not_create_transaction(self, session: Session) -> None:
        """套用模板**不写流水**：金额可能是空的，而且用户总要先看一眼。"""
        from accountbook.services import transactions as transactions_service
        from accountbook.services.transactions import TransactionQuery

        template = self._template(session)
        templates_service.apply_template(session, template.id)
        assert transactions_service.list_transactions(session, TransactionQuery(limit=10))[1] == 0

    def test_update_and_delete(self, session: Session) -> None:
        template = self._template(session)
        templates_service.update_template(session, template.id, name="午餐（改）", amount_minor=4_200)
        session.refresh(template)
        assert template.name == "午餐（改）"
        assert template.amount_minor == 4_200

        templates_service.delete_template(session, template.id)
        assert templates_service.list_templates(session) == []
        with pytest.raises(NotFoundError):
            templates_service.get_template(session, template.id)

    def test_broken_tag_json_does_not_break_listing(self, session: Session) -> None:
        """存坏了的 tag_ids 不该让整个列表读不出来。"""
        template = self._template(session)
        template.tag_ids = "{不是 JSON"
        session.flush()
        assert templates_service.serialize(template)["tag_ids"] == []
        assert len(templates_service.list_templates(session)) == 1


class TestParseAmount:
    def test_plain_number(self) -> None:
        amount, hit, rest = templates_service.parse_amount("永辉超市 58.30")
        assert amount == 5_830
        assert hit == "58.30"
        assert "58.30" not in rest

    def test_currency_symbol(self) -> None:
        assert templates_service.parse_amount("¥128")[0] == 12_800
        assert templates_service.parse_amount("128元")[0] == 12_800
        assert templates_service.parse_amount("128块")[0] == 12_800

    def test_thousands_separator(self) -> None:
        assert templates_service.parse_amount("1,234.56")[0] == 123_456

    def test_units(self) -> None:
        """3万 = 30000，1.5万 = 15000，2千 = 2000。"""
        assert templates_service.parse_amount("押金 3万")[0] == 3_000_000
        assert templates_service.parse_amount("1.5万")[0] == 1_500_000
        assert templates_service.parse_amount("2千")[0] == 200_000

    def test_negative_sign(self) -> None:
        assert templates_service.parse_amount("-88")[0] == -8_800

    def test_chinese_numeral(self) -> None:
        assert templates_service.parse_amount("五块")[0] == 500

    def test_only_first_amount(self) -> None:
        """一段话里多个数字时只取第一个并交给用户确认 —— 猜哪个是金额必然出错。"""
        amount, _, rest = templates_service.parse_amount("买 3 件 共 58 元")
        assert amount == 300
        assert "58" in rest

    def test_no_amount(self) -> None:
        assert templates_service.parse_amount("永辉超市")[0] is None


class TestParseDate:
    def test_relative_words(self) -> None:
        today = date(2026, 10, 1)
        value, hit, _ = templates_service.parse_date("昨天 午饭 38", today=today)
        assert value is not None and value.date() == date(2026, 9, 30)
        assert hit == "昨天"

        tomorrow, _, _ = templates_service.parse_date("明天", today=today)
        assert tomorrow is not None and tomorrow.date() == date(2026, 10, 2)

    def test_month_day(self) -> None:
        value, _, _ = templates_service.parse_date("10月1日 房租 4500", today=date(2026, 1, 1))
        assert value is not None
        assert value.date() == date(2026, 10, 1)

    def test_iso_date(self) -> None:
        value, _, _ = templates_service.parse_date("2026-09-15 打车 24.5", today=TODAY)
        assert value is not None and value.date() == date(2026, 9, 15)

    def test_clock(self) -> None:
        value, _, _ = templates_service.parse_date("今天 18:30 晚饭 66", today=TODAY)
        assert value is not None
        assert (value.hour, value.minute) == (18, 30)

    def test_no_date_returns_none(self) -> None:
        """没给日期时返回 None，由调用方决定 —— 在这里默认成今天会覆盖"明天"。"""
        assert templates_service.parse_date("午饭 38", today=TODAY)[0] is None

    def test_impossible_date_is_not_guessed(self) -> None:
        assert templates_service.parse_date("2月30日 买菜", today=TODAY)[0] is None


class TestParseQuickText:
    def test_full_sentence(self, session: Session) -> None:
        result = templates_service.parse_quick_text(session, "今天 午餐 38 微信", today=TODAY)
        assert result["type"] == "expense"
        assert result["amount_minor"] == 3_800
        assert result["occurred_at"] is not None
        assert result["category_id"] == _category(session, "午餐")
        # "微信" 是账户名
        wechat = next(item for item in accounts_service.list_accounts(session) if item.name == "微信")
        assert result["account_id"] == wechat.id

    def test_income_direction(self, session: Session) -> None:
        result = templates_service.parse_quick_text(session, "工资 +18500 工资卡", today=TODAY)
        assert result["type"] == "income"
        assert result["amount_minor"] == 1_850_000

    def test_negative_sign_means_expense(self, session: Session) -> None:
        result = templates_service.parse_quick_text(session, "-38 午餐", today=TODAY)
        assert result["type"] == "expense"
        assert result["amount_minor"] == 3_800, "符号用于判断方向后应当取绝对值"

    def test_direction_is_not_guessed(self, session: Session) -> None:
        """既无符号也无方向词时**不猜** —— 猜错会把支出变成收入。"""
        result = templates_service.parse_quick_text(session, "38", today=TODAY)
        assert result["type"] is None
        assert result["amount_minor"] == 3_800

    def test_unmatched_words_are_returned(self, session: Session) -> None:
        """认不出的词必须原样交还：界面要显示它，用户才知道哪些没被采纳。"""
        result = templates_service.parse_quick_text(session, "昨天 永辉超市 会员日 128.5 支付宝", today=TODAY)
        assert result["amount_minor"] == 12_850
        # 第一个剩余片段作为商户名，其余进 unmatched
        assert result["payee"]
        assert "会员日" in result["unmatched"] or "会员日" in result["payee"]

    def test_longest_name_wins(self, session: Session) -> None:
        """同时存在"招商银行"与"招商银行信用卡"时，长的那个必须优先命中。

        刻意自己建这两个账户，而不是指望种子数据里恰好有这样一对 ——
        依赖种子内容的测试会在别人调整种子时莫名其妙地失败。
        """
        accounts_service.create_account(session, name="招商银行", type="debit_card")
        card = accounts_service.create_account(session, name="招商银行信用卡", type="credit_card")
        result = templates_service.parse_quick_text(session, f"超市 88 {card.name}", today=TODAY)
        assert result["account_id"] == card.id

    def test_empty_text_rejected(self, session: Session) -> None:
        with pytest.raises(ValidationError):
            templates_service.parse_quick_text(session, "   ")

    def test_raw_is_echoed(self, session: Session) -> None:
        result = templates_service.parse_quick_text(session, "午饭 38", today=TODAY)
        assert result["raw"] == "午饭 38"
        assert isinstance(result["occurred_at"], (datetime, type(None)))
