"""记账模板与剪贴板/自然语言解析（P1 收尾）。

两个功能
--------
1. **模板**：把一整套字段存下来，一键填完表单。模板栏按"最常用"排序 ——
   按创建时间排序会让第三个月建的模板永远排在最后，而用户最想点的
   恰恰是天天用的那几个。
2. **解析一段文本**：从剪贴板或随手输入里抽出金额、日期、账户、分类。
   场景很具体：银行/微信的扣款短信、别人发来的"午饭 38"、
   自己写的购物清单。

解析的设计原则
--------------
* **绝不猜**。认不出的部分放进 ``unmatched`` 原样交还给用户，
  而不是硬塞给某个字段。一个"猜错了但看起来填好了"的表单
  比空表单糟糕得多 —— 用户会直接保存，然后账目就错了。
* **返回草稿而不是直接写库**。解析结果一律进表单让用户确认，
  这是本模块唯一安全的用法。
* **金额是整数最小单位**，解析在边界处用 Decimal 完成，
  之后立刻转成 int（与全项目一致）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.domain import TransactionType
from ..core.errors import NotFoundError, ValidationError
from ..core.money import DEFAULT_CURRENCY, to_minor
from ..db.base import utc_now
from ..db.models import Account, Category, Member, Project, Tag, TransactionTemplate
from . import audit

__all__ = [
    "apply_template",
    "create_template",
    "delete_template",
    "get_template",
    "list_templates",
    "parse_quick_text",
    "update_template",
]

_logger = logging.getLogger(__name__)

_MUTABLE = frozenset(
    {
        "name",
        "type",
        "account_id",
        "to_account_id",
        "category_id",
        "amount_minor",
        "currency",
        "payee",
        "note",
        "tag_ids",
        "project_id",
        "member_id",
        "sort_order",
    }
)


# -----------------------------------------------------------------------------
# 模板
# -----------------------------------------------------------------------------
def list_templates(session: Session) -> list[TransactionTemplate]:
    """模板列表，按"最常用"排序。

    排序键：使用次数降序 → 最近使用降序 → sort_order。
    没用过的模板（usage_count=0）按用户手动排的顺序排在最前，
    否则新用户会看到一个空白的模板栏。
    """
    rows = list(
        session.scalars(
            select(TransactionTemplate)
            .where(TransactionTemplate.deleted_at.is_(None))
            .order_by(
                TransactionTemplate.usage_count.desc(),
                TransactionTemplate.last_used_at.desc().nullslast(),
                TransactionTemplate.sort_order,
                TransactionTemplate.id,
            )
        ).all()
    )
    return rows


def get_template(session: Session, template_id: int) -> TransactionTemplate:
    template = session.get(TransactionTemplate, template_id)
    if template is None or template.deleted_at is not None:
        raise NotFoundError("模板不存在", entity="transaction_template", entity_id=template_id)
    return template


def serialize(template: TransactionTemplate) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "type": template.type,
        "account_id": template.account_id,
        "to_account_id": template.to_account_id,
        "category_id": template.category_id,
        "amount_minor": template.amount_minor,
        "currency": template.currency,
        "payee": template.payee,
        "note": template.note,
        "tag_ids": _load_tag_ids(template),
        "project_id": template.project_id,
        "member_id": template.member_id,
        "sort_order": template.sort_order,
        "usage_count": template.usage_count,
        "last_used_at": template.last_used_at.isoformat() if template.last_used_at else None,
    }


def _load_tag_ids(template: TransactionTemplate) -> list[int]:
    try:
        value = json.loads(template.tag_ids or "[]")
    except json.JSONDecodeError:
        # 存坏了不该让整个列表读不出来：当空处理并留一条日志
        _logger.warning("模板 %s 的 tag_ids 不是合法 JSON，按空处理", template.id)
        return []
    return [int(item) for item in value if isinstance(item, (int, str)) and str(item).isdigit()]


def _validate(session: Session, payload: dict[str, Any]) -> None:
    template_type = payload.get("type")
    if template_type is not None and template_type not in {
        TransactionType.EXPENSE.value,
        TransactionType.INCOME.value,
        TransactionType.TRANSFER.value,
    }:
        raise ValidationError(f"模板不支持的类型：{template_type}", field="type")
    if payload.get("type") == TransactionType.TRANSFER.value and not payload.get("to_account_id"):
        raise ValidationError("转账模板必须指定目标账户", field="to_account_id")

    amount = payload.get("amount_minor")
    if amount is not None and amount < 0:
        raise ValidationError("金额不能为负数", field="amount_minor")

    for field, model, label in (
        ("account_id", Account, "账户"),
        ("to_account_id", Account, "目标账户"),
        ("category_id", Category, "分类"),
        ("project_id", Project, "项目"),
        ("member_id", Member, "成员"),
    ):
        entity_id = payload.get(field)
        if entity_id is None:
            continue
        entity = session.get(model, entity_id)
        if entity is None or getattr(entity, "deleted_at", None) is not None:
            raise NotFoundError(f"{label}不存在", entity=field, entity_id=entity_id)

    # ``tag_ids`` 可能以两种形态到达：请求体里是列表，
    # 而从模型直接读出来的（更新时的 merged）是 JSON 文本。
    # 不归一化就会把字符串 "[]" 塞进 IN 子句，得到一个 SQL 层的报错。
    tag_ids = payload.get("tag_ids")
    if isinstance(tag_ids, str):
        try:
            tag_ids = json.loads(tag_ids or "[]")
        except json.JSONDecodeError:
            tag_ids = []
    if tag_ids:
        found = session.scalars(select(Tag.id).where(Tag.id.in_(tag_ids))).all()
        missing = set(tag_ids) - set(found)
        if missing:
            raise NotFoundError("标签不存在", entity="tag", entity_id=sorted(missing)[0])


def create_template(session: Session, **payload: Any) -> TransactionTemplate:
    unknown = set(payload) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的模板字段：{sorted(unknown)}", fields=sorted(unknown))
    _validate(session, payload)

    tag_ids = payload.pop("tag_ids", None)
    template = TransactionTemplate(**payload)
    if tag_ids is not None:
        template.tag_ids = json.dumps(sorted({int(item) for item in tag_ids}))
    session.add(template)
    session.flush()
    audit.record(
        session,
        entity="transaction_template",
        entity_id=template.id,
        action="create",
        changes={"name": {"to": template.name}},
    )
    return template


def update_template(session: Session, template_id: int, **changes: Any) -> TransactionTemplate:
    unknown = set(changes) - _MUTABLE
    if unknown:
        raise ValidationError(f"不支持的模板字段：{sorted(unknown)}", fields=sorted(unknown))
    template = get_template(session, template_id)

    tag_ids = changes.pop("tag_ids", None)
    merged = {field: getattr(template, field) for field in _MUTABLE}
    if tag_ids is not None:
        merged["tag_ids"] = tag_ids
    merged.update(changes)
    _validate(session, merged)

    before = audit.snapshot(template, changes.keys())
    for key, value in changes.items():
        setattr(template, key, value)
    if tag_ids is not None:
        template.tag_ids = json.dumps(sorted({int(item) for item in tag_ids}))
    session.flush()
    audit.record_diff(
        session,
        entity="transaction_template",
        entity_id=template_id,
        action="update",
        before=before,
        after={key: getattr(template, key) for key in changes},
    )
    return template


def delete_template(session: Session, template_id: int) -> None:
    template = get_template(session, template_id)
    template.soft_delete()
    session.flush()
    audit.record(session, entity="transaction_template", entity_id=template_id, action="delete")


def apply_template(session: Session, template_id: int) -> dict[str, Any]:
    """套用模板：返回一份**可直接填进表单**的草稿，并记一次使用。

    刻意不直接创建流水：模板的金额可能是空的（"只填结构"），
    而且用户几乎总要先看一眼再保存。
    """
    template = get_template(session, template_id)
    template.usage_count = (template.usage_count or 0) + 1
    template.last_used_at = utc_now()
    session.flush()
    return serialize(template)


# -----------------------------------------------------------------------------
# 剪贴板 / 自然语言解析
# -----------------------------------------------------------------------------
# 金额：¥12.5 / 12.5元 / 1,234.56 / 3万 / 2千
#
# 千分位分支用 ``+`` 而不是 ``*``：用 ``*`` 时 "18500" 会被这个分支
# 匹配成 "185"（逗号组出现零次也算命中），185 元直接变成 1.85 元。
# 纯数字串必须落到第二个分支上去。
_AMOUNT_RE = re.compile(
    r"(?P<sign>[+\-])?\s*[¥￥]?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<unit>万|千|百|元|块|圆)?"
)

#: 中文数字（只处理几百几千这类常见写法，不做完整的中文数字解析）
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

#: 明确表示"支出"的词。没有这些词时**不猜方向**，交给用户选
_EXPENSE_HINTS = ("买", "花", "付", "交", "充", "缴", "退", "扣", "消费", "支出", "打车", "吃", "喝")
_INCOME_HINTS = ("收", "工资", "奖金", "报销", "退款", "利息", "分红", "红包", "入账", "收入", "补贴")

_DATE_WORDS = {
    "今天": 0,
    "今日": 0,
    "昨天": -1,
    "昨日": -1,
    "前天": -2,
    "明天": 1,
    "明日": 1,
    "后天": 2,
}
_DATE_RE = re.compile(r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?")
_TIME_RE = re.compile(r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})")


def _cn_or_arabic(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    total = 0
    for char in token:
        if char in _CN_DIGITS:
            total = total * 10 + _CN_DIGITS[char]
        else:
            return None
    return total or None


def parse_amount(text: str) -> tuple[int | None, str, str]:
    """抽金额。返回 ``(最小单位金额, 命中的原文, 剩下的文本)``。

    单位处理："3万" = 30000，"2千" = 2000，"1.5万" = 15000。
    元/块/圆 只是量词，不影响数值。

    只取**第一个**金额：一段话里出现多个数字时（"买 3 件共 58 元"），
    猜哪个是金额必然出错，宁可让用户改。命中位置会被从文本里移除，
    这样剩余的文本能直接当作商户名。
    """
    for match in _AMOUNT_RE.finditer(text):
        raw = match.group("num").replace(",", "")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        unit = match.group("unit")
        if unit == "万":
            value *= 10_000
        elif unit == "千":
            value *= 1_000
        elif unit == "百":
            value *= 100
        # 单位是"元/块"或没有单位时才接受：没有单位且后面紧跟中文，
        # 很可能是"买 3 件"这类数量词，但无法可靠区分，因此仍接受并让用户确认
        if value <= 0:
            continue
        sign = -1 if match.group("sign") == "-" else 1
        remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
        return sign * to_minor(value), match.group(0).strip(), remainder

    # 没有阿拉伯数字时试一下纯中文数字（"五块"）
    for token in re.findall(r"[零一二两三四五六七八九十百千]+", text):
        value = _cn_or_arabic(token)
        if value:
            remainder = text.replace(token, " ", 1).strip()
            return to_minor(Decimal(value)), token, remainder
    return None, "", text


def parse_date(text: str, *, today: date | None = None) -> tuple[datetime | None, str, str]:
    """抽日期与时间。返回 ``(业务时间, 命中原文, 剩余文本)``。

    支持：今天/昨天/前天/明天/后天、10月1日、2026-10-01、以及 HH:MM。
    没给日期时返回 ``None``，由调用方决定（通常是"就是现在"）——
    在本函数里默认成今天会让"明天"这类词被悄悄覆盖。
    """
    base = today or date.today()
    resolved: date | None = None
    hit = ""

    for word, offset in _DATE_WORDS.items():
        if word in text:
            resolved = base + timedelta(days=offset)
            hit = word
            break

    if resolved is None:
        match = _DATE_RE.search(text)
        if match:
            year = int(match.group("year")) if match.group("year") else base.year
            month = int(match.group("month"))
            day = int(match.group("day"))
            try:
                resolved = date(year, month, day)
            except ValueError:
                # 2 月 30 日之类：不猜，原样留给用户
                return None, "", text
            hit = match.group(0)

    clock: time | None = None
    time_match = _TIME_RE.search(text)
    if time_match:
        hour = int(time_match.group("hour"))
        minute = int(time_match.group("minute"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            clock = time(hour, minute)
            hit = f"{hit} {time_match.group(0)}".strip()

    if resolved is None and clock is None:
        return None, "", text
    remainder = text.replace(hit, " ", 1).strip() if hit else text
    if time_match and time_match.group(0) in remainder:
        remainder = remainder.replace(time_match.group(0), " ", 1).strip()
    return datetime.combine(resolved or base, clock or time(hour=12)), hit, remainder


def _match_named(session: Session, text: str) -> tuple[Any | None, str]:
    """按名字匹配账户或分类（最长匹配优先）。

    最长优先很重要：账本里同时有"招商银行"和"招商银行信用卡"时，
    短的那个会先把"招商银行"吃掉，剩下"信用卡"变成商户名。
    """
    from ..db.models import Account as AccountModel
    from ..db.models import Category as CategoryModel

    candidates: list[tuple[str, Any]] = []
    for row in session.scalars(select(AccountModel).where(AccountModel.deleted_at.is_(None))).all():
        candidates.append((row.name, row))
    for row in session.scalars(select(CategoryModel).where(CategoryModel.deleted_at.is_(None))).all():
        candidates.append((row.name, row))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    for name, row in candidates:
        if name and name in text:
            return row, name
    return None, ""


def parse_quick_text(session: Session, text: str, *, today: date | None = None) -> dict[str, Any]:
    """把一段文本解析成记账草稿。

    返回的 ``unmatched`` 是**必须展示**的：认不出的词原样交还，
    而不是硬塞进备注。用户看到"永辉超市"没被认出来，就知道要手动选一下。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValidationError("没有可解析的内容", field="text")

    remaining = raw
    amount_minor, amount_hit, remaining = parse_amount(remaining)
    occurred_at, date_hit, remaining = parse_date(remaining, today=today)

    account, account_hit = _match_named(session, remaining)
    if account_hit:
        remaining = remaining.replace(account_hit, " ", 1).strip()
    category, category_hit = _match_named(session, remaining)
    if category_hit:
        remaining = remaining.replace(category_hit, " ", 1).strip()

    # 方向：优先用金额的符号，其次看关键字。
    # 两者都没有时**不猜**，把 type 留空让用户选 —— 猜错方向会让一笔
    # 支出变成收入，是这里最严重的可能错误
    transaction_type: str | None = None
    if amount_minor is not None and amount_minor < 0:
        transaction_type = TransactionType.EXPENSE.value
        amount_minor = abs(amount_minor)
    elif any(hint in raw for hint in _INCOME_HINTS):
        transaction_type = TransactionType.INCOME.value
    elif any(hint in raw for hint in _EXPENSE_HINTS):
        transaction_type = TransactionType.EXPENSE.value

    # 账户与分类可能被同一个名字命中，按实体类型分开
    resolved_account = account if isinstance(account, Account) else None
    resolved_category = category if isinstance(category, Category) else None
    if isinstance(account, Category) and resolved_category is None:
        resolved_category = account
    if isinstance(category, Account) and resolved_account is None:
        resolved_account = category

    # 分类自带方向：命中一个支出分类时方向就是确定的，
    # 这不是"猜"，而是读取分类自身的 kind。放在最后是为了让
    # 金额符号与方向关键字优先（它们表达的是用户的明确意图）
    if transaction_type is None and resolved_category is not None:
        transaction_type = getattr(resolved_category, "kind", None)

    # 剩下的非空片段当作商户名 —— 这部分是**低置信度**的，
    # 因此放在 payee 里（可直接改），而不是 note
    leftovers = [part for part in re.split(r"[\s,，、;；]+", remaining) if part]
    payee = leftovers[0][:64] if leftovers else ""

    return {
        "type": transaction_type,
        "amount_minor": amount_minor,
        "occurred_at": occurred_at,
        "account_id": getattr(resolved_account, "id", None),
        "category_id": getattr(resolved_category, "id", None),
        "payee": payee,
        # 备注留空：解析出的词已经进了 payee，塞进备注只会让用户在两个地方各看到半截信息
        "note": "",
        "currency": DEFAULT_CURRENCY,
        "matched": {
            "amount": amount_hit,
            "date": date_hit,
            "account": account_hit if resolved_account is not None else "",
            "category": category_hit if resolved_category is not None else "",
            "payee": payee,
        },
        # 认不出的词原样返回，界面必须显示出来，让用户知道哪些没被采纳
        "unmatched": leftovers[1:] if len(leftovers) > 1 else [],
        "raw": raw,
    }
