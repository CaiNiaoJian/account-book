"""资产与卡片墙服务（REQ-15）。

它比 ``services/accounts.py`` 多做的事
-------------------------------------
``accounts.overview`` 回答"我有多少钱"；本模块回答"这些钱放在哪、长什么样"：

* 按**用途分组**（银行卡 / 电子钱包 / 投资 / 负债 / 其它）而不是按数据库里的
  类型枚举顺序 —— 用户理解的"我的卡"是按使用场景分的；
* 带上卡面信息（机构、卡组织、卡面主题、色调），让前端能画出卡；
* 计算**可用资金**与**信用可用额度**这两个真正会被用到的数字；
* 支持拖拽排序（一次提交整组顺序，避免逐条 PATCH 产生 N 次写入）。

余额仍然来自 ``accounts`` 服务的聚合 —— 这里不重复实现一遍口径。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.domain import AccountType
from ..core.errors import ConflictError, NotFoundError, ProtectedEntityError, ValidationError
from ..db.models import Account, Attachment, CardArtwork, Institution
from . import accounts as accounts_service
from . import audit

__all__ = [
    "ACCOUNT_GROUPS",
    "CARD_MUTABLE_FIELDS",
    "GROUP_OF_TYPE",
    "create_card_artwork",
    "create_institution",
    "delete_card_artwork",
    "delete_institution",
    "list_card_artworks",
    "list_institutions",
    "reorder_accounts",
    "update_account_cards",
    "update_card_artwork",
    "update_institution",
    "wall",
]

_logger = logging.getLogger(__name__)

#: 卡片墙分组顺序。**顺序即展示顺序**，与用户的心理模型一致：
#: 先看能花的钱，再看投资，最后看欠款。
ACCOUNT_GROUPS: tuple[tuple[str, str], ...] = (
    ("bank", "银行卡"),
    ("wallet", "电子钱包"),
    ("cash", "现金"),
    ("investment", "投资"),
    ("liability", "负债"),
    ("other", "其它"),
)

#: 账户类型 → 分组
GROUP_OF_TYPE: dict[str, str] = {
    AccountType.DEBIT_CARD.value: "bank",
    AccountType.CREDIT_CARD.value: "bank",
    AccountType.E_WALLET.value: "wallet",
    AccountType.PREPAID.value: "wallet",
    AccountType.CASH.value: "cash",
    AccountType.INVESTMENT.value: "investment",
    AccountType.RECEIVABLE.value: "investment",
    AccountType.PAYABLE.value: "liability",
    AccountType.VIRTUAL.value: "other",
}

#: 可通过卡片墙编辑的字段（其余仍走账户接口）
CARD_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {"brand_key", "card_style", "card_network", "theme_tint", "card_no_tail", "sort_order"}
)

_CARD_NETWORKS = ("", "unionpay", "visa", "mastercard", "amex", "jcb")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


# -----------------------------------------------------------------------------
# 卡片墙
# -----------------------------------------------------------------------------
def wall(session: Session, *, include_archived: bool = False) -> dict[str, Any]:
    """卡片墙数据：分组后的卡片 + 资产概览 + 卡面字典。

    一次返回全部内容而不是让前端发三个请求：卡片墙是一个整体视图，
    分三批到达会让卡片先出现、颜色后到位，看起来像加载失败过。
    """
    overview = accounts_service.overview(session, include_archived=include_archived)
    accounts = list(
        session.scalars(
            select(Account).where(Account.deleted_at.is_(None)).order_by(Account.sort_order, Account.id)
        ).all()
    )
    institutions = {item.key: item for item in list_institutions(session)}
    accounts_by_id = {account.id: account for account in accounts}
    all_artworks = list_card_artworks(session)
    # 一次查出所有卡面图片，避免"列出 8 个卡面"变成 8 次查询
    artwork_images = {
        row.card_artwork_id: f"/api/attachments/{row.id}"
        for row in session.scalars(
            select(Attachment)
            .where(Attachment.kind == "card", Attachment.card_artwork_id.is_not(None))
            .order_by(Attachment.id)
        ).all()
    }

    groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in ACCOUNT_GROUPS}
    available_minor = 0
    credit_limit_minor = 0
    credit_used_minor = 0

    for item in overview["accounts"]:
        account = accounts_by_id.get(item["id"])
        if account is None:
            continue
        if not include_archived and account.is_archived:
            continue

        group_key = GROUP_OF_TYPE.get(account.type, "other")
        institution = institutions.get(account.brand_key)
        card = {
            **item,
            "brand_key": account.brand_key,
            "brand_name": institution.name if institution else account.institution,
            "brand_color": institution.brand_color if institution else account.color,
            "card_style": account.card_style,
            "card_network": account.card_network,
            "theme_tint": account.theme_tint,
            "bill_day": account.bill_day,
            "due_day": account.due_day,
            "group": group_key,
        }
        # 信用卡：可用额度 = 额度 - 已用（余额为负表示欠款）
        if account.type == AccountType.CREDIT_CARD.value and account.credit_limit_minor > 0:
            used = max(0, -item["balance_minor"])
            card["credit_used_minor"] = used
            card["credit_available_minor"] = max(0, account.credit_limit_minor - used)
            credit_limit_minor += account.credit_limit_minor
            credit_used_minor += used
        else:
            card["credit_used_minor"] = 0
            card["credit_available_minor"] = 0

        # 可用资金：能立刻花的钱（现金 + 储蓄卡 + 电子钱包 + 储值卡的正余额）
        if group_key in {"cash", "wallet"} or account.type == AccountType.DEBIT_CARD.value:
            available_minor += max(0, item["balance_minor"])

        groups[group_key].append(card)

    return {
        "groups": [
            {"key": key, "name": name, "cards": groups[key]} for key, name in ACCOUNT_GROUPS if groups[key]
        ],
        "summary": {
            **{key: value for key, value in overview.items() if key != "accounts"},
            "available_minor": available_minor,
            "credit_limit_minor": credit_limit_minor,
            "credit_used_minor": credit_used_minor,
            "credit_available_minor": max(0, credit_limit_minor - credit_used_minor),
        },
        "institutions": [serialize_institution(item) for item in institutions.values()],
        "artworks": [serialize_artwork(item, image_url=artwork_images.get(item.id)) for item in all_artworks],
    }


def reorder_accounts(session: Session, order: list[int]) -> int:
    """按给定顺序重排账户（一次提交整组顺序）。

    做成"整体提交"而不是"逐条 PATCH"：拖拽会产生一次完整的新顺序，
    逐条写入既慢又可能出现中间态（前端刷新时看到半截顺序）。
    """
    accounts = {
        account.id: account
        for account in session.scalars(
            select(Account).where(Account.deleted_at.is_(None), Account.id.in_(order))
        ).all()
    }
    missing = [item for item in order if item not in accounts]
    if missing:
        raise NotFoundError("部分账户不存在或已被删除", entity="account", entity_id=missing[0])

    for index, account_id in enumerate(order):
        accounts[account_id].sort_order = index * 10
    session.flush()
    # 顺序变化不改变净值，因此**不需要**重算日结 —— 这是拖拽能即时响应的原因
    audit.record(
        session,
        entity="account",
        entity_id=None,
        action="reorder",
        changes={"order": {"to": f"<{len(order)} 项>"}},
    )
    return len(order)


def update_account_cards(session: Session, account_id: int, **changes: Any) -> Account:
    """更新卡片外观（卡面 / 机构 / 卡组织 / 色调）。"""
    unknown = set(changes) - CARD_MUTABLE_FIELDS
    if unknown:
        raise ValidationError(f"不支持的卡片字段：{sorted(unknown)}", fields=sorted(unknown))

    account = accounts_service.get_account(session, account_id)
    if "card_network" in changes and changes["card_network"] not in _CARD_NETWORKS:
        raise ValidationError(f"未知卡组织：{changes['card_network']}", field="card_network")
    if changes.get("brand_key"):
        institution = session.scalar(
            select(Institution).where(
                Institution.key == changes["brand_key"], Institution.deleted_at.is_(None)
            )
        )
        if institution is None:
            raise NotFoundError("机构不存在", entity="institution", key=changes["brand_key"])
        # 让卡片墙上的机构名与字典保持一致，避免两处显示不同名
        account.institution = institution.name
    if changes.get("card_style"):
        artwork = session.scalar(select(CardArtwork).where(CardArtwork.key == changes["card_style"]))
        if artwork is None:
            raise NotFoundError("卡面不存在", entity="card_artwork", key=changes["card_style"])

    before = audit.snapshot(account, changes.keys())
    for key, value in changes.items():
        setattr(account, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity="account",
        entity_id=account_id,
        action="update",
        before=before,
        after={key: getattr(account, key) for key in changes},
    )
    return account


# -----------------------------------------------------------------------------
# 机构字典
# -----------------------------------------------------------------------------
def serialize_institution(item: Institution) -> dict[str, Any]:
    return {
        "id": item.id,
        "key": item.key,
        "name": item.name,
        "kind": item.kind,
        "brand_color": item.brand_color,
        "logo_ref": item.logo_ref,
        "is_system": item.is_system,
        "sort_order": item.sort_order,
    }


def list_institutions(session: Session, *, include_deleted: bool = False) -> list[Institution]:
    statement = select(Institution)
    if not include_deleted:
        statement = statement.where(Institution.deleted_at.is_(None))
    return list(session.scalars(statement.order_by(Institution.sort_order, Institution.id)).all())


def create_institution(
    session: Session, *, key: str, name: str, kind: str = "bank", brand_color: str = "accent"
) -> Institution:
    """新增用户自定义机构。

    ``key`` 必须是稳定的英文短标识：它会写进账户的 ``brand_key``，
    而名称是可以随时改的（改名不该让卡面失效）。
    """
    key = (key or "").strip().lower()
    if not _KEY_PATTERN.match(key):
        raise ValidationError(
            "机构键必须是 2–32 位小写字母/数字/下划线，且以字母开头", field="key", value=key
        )
    name = (name or "").strip()
    if not name:
        raise ValidationError("机构名称不能为空", field="name")

    existing = session.scalar(
        select(Institution).where(Institution.key == key, Institution.deleted_at.is_(None))
    )
    if existing is not None:
        raise ConflictError(f"机构键已存在：{key}", field="key", value=key)

    institution = Institution(key=key, name=name, kind=kind, brand_color=brand_color)
    session.add(institution)
    session.flush()
    audit.record(
        session,
        entity="institution",
        entity_id=institution.id,
        action="create",
        changes={"key": {"to": key}, "name": {"to": name}},
    )
    return institution


def update_institution(session: Session, institution_id: int, **changes: Any) -> Institution:
    allowed = {"name", "kind", "brand_color", "logo_ref", "sort_order"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"不支持的机构字段：{sorted(unknown)}", fields=sorted(unknown))

    institution = session.get(Institution, institution_id)
    if institution is None or institution.deleted_at is not None:
        raise NotFoundError("机构不存在", entity="institution", entity_id=institution_id)

    before = audit.snapshot(institution, changes.keys())
    for key, value in changes.items():
        setattr(institution, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity="institution",
        entity_id=institution_id,
        action="update",
        before=before,
        after={key: getattr(institution, key) for key in changes},
    )
    return institution


def delete_institution(session: Session, institution_id: int) -> None:
    """删除机构。

    内置机构不可删（它们承担"开箱可用"的职责）；
    被账户引用的机构也不可删 —— 否则那些卡会失去机构名与配色。
    """
    institution = session.get(Institution, institution_id)
    if institution is None or institution.deleted_at is not None:
        raise NotFoundError("机构不存在", entity="institution", entity_id=institution_id)
    if institution.is_system:
        raise ProtectedEntityError(
            "内置机构不可删除，可以改名或改主色",
            institution_id=institution_id,
            suggestion="rename",
        )

    used = session.scalar(
        select(func.count(Account.id)).where(
            Account.brand_key == institution.key, Account.deleted_at.is_(None)
        )
    )
    if used:
        raise ConflictError(
            f"该机构已被 {int(used or 0)} 个账户使用，请先更换这些账户的机构",
            institution_id=institution_id,
            account_count=int(used or 0),
        )

    institution.soft_delete()
    session.flush()
    audit.record(session, entity="institution", entity_id=institution_id, action="delete")


# -----------------------------------------------------------------------------
# 卡面
# -----------------------------------------------------------------------------
def serialize_artwork(item: CardArtwork, *, image_url: str | None = None) -> dict[str, Any]:
    """卡面。

    ``image_url`` 由调用方注入（``wall`` 会一次查出全部卡面图片）——
    让本函数自己去查附件表会在"列出 8 个卡面"时产生 8 次查询，
    而且会把 assets 服务与附件表绑在一起。
    """
    return {
        "id": item.id,
        "key": item.key,
        "name": item.name,
        "kind": item.kind,
        "spec": item.spec,
        "file_ref": item.file_ref,
        # 用户上传的卡面图片地址；为 None 表示用 spec 里的自绘渐变
        "image_url": image_url,
        "author": item.author,
        "license": item.license,
        "sort_order": item.sort_order,
    }


def list_card_artworks(session: Session) -> list[CardArtwork]:
    return list(session.scalars(select(CardArtwork).order_by(CardArtwork.sort_order, CardArtwork.id)).all())


def _validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """校验卡面配方。

    卡面是**数据驱动的自绘图形**（渐变 + 纹理 + 光泽），因此 spec 的字段必须受限：
    如果允许任意 JSON，将来渲染端要做一堆防御性判断，
    而一个拼错的键只会让卡片变白 —— 那种失败很难定位。
    """
    if not isinstance(spec, dict):
        raise ValidationError("卡面配方必须是对象", field="spec")

    stops = spec.get("stops")
    if not isinstance(stops, list) or len(stops) < 2:
        raise ValidationError("卡面配方至少需要两个渐变色标", field="spec.stops")
    for index, stop in enumerate(stops):
        if not isinstance(stop, list) or len(stop) != 2:
            raise ValidationError("色标格式应为 [颜色, 百分比]", index=index)
        color, position = stop
        if not isinstance(color, str) or not re.match(r"^#[0-9a-fA-F]{6}$", color):
            raise ValidationError(f"色标颜色必须是 #RRGGBB：{color!r}", index=index)
        if not isinstance(position, (int, float)) or not (0 <= float(position) <= 100):
            raise ValidationError(f"色标位置必须在 0–100：{position!r}", index=index)

    texture = spec.get("texture", "none")
    if texture not in {"none", "grain", "aurora", "soft"}:
        raise ValidationError(f"未知纹理：{texture}", field="spec.texture")

    ink = spec.get("ink", "light")
    if ink not in {"light", "dark"}:
        raise ValidationError(f"未知文字色：{ink}", field="spec.ink")

    sheen = spec.get("sheen", 120)
    if not isinstance(sheen, (int, float)) or not (0 <= float(sheen) <= 360):
        raise ValidationError("光泽角度必须在 0–360 度", field="spec.sheen")

    return {
        "stops": [[stop[0], float(stop[1])] for stop in stops],
        "texture": texture,
        "ink": ink,
        "sheen": float(sheen),
    }


def create_card_artwork(
    session: Session,
    *,
    key: str,
    name: str,
    spec: dict[str, Any],
    kind: str = "uploaded",
    file_ref: str = "",
    author: str = "用户",
    license: str = "用户自有",
) -> CardArtwork:
    key = (key or "").strip().lower()
    if not _KEY_PATTERN.match(key):
        raise ValidationError("卡面键必须是 2–32 位小写字母/数字/下划线", field="key", value=key)
    name = (name or "").strip()
    if not name:
        raise ValidationError("卡面名称不能为空", field="name")
    if session.scalar(select(CardArtwork.id).where(CardArtwork.key == key)) is not None:
        raise ConflictError(f"卡面键已存在：{key}", field="key", value=key)

    artwork = CardArtwork(
        key=key,
        name=name,
        kind=kind if kind in {"builtin", "uploaded"} else "uploaded",
        spec=_validate_spec(spec),
        file_ref=file_ref,
        author=author,
        license=license,
    )
    session.add(artwork)
    session.flush()
    audit.record(
        session,
        entity="card_artwork",
        entity_id=artwork.id,
        action="create",
        changes={"key": {"to": key}},
    )
    return artwork


def update_card_artwork(session: Session, artwork_id: int, **changes: Any) -> CardArtwork:
    allowed = {"name", "spec", "file_ref", "sort_order"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"不支持的卡面字段：{sorted(unknown)}", fields=sorted(unknown))

    artwork = session.get(CardArtwork, artwork_id)
    if artwork is None:
        raise NotFoundError("卡面不存在", entity="card_artwork", entity_id=artwork_id)
    if "spec" in changes:
        changes["spec"] = _validate_spec(changes["spec"])

    before = audit.snapshot(artwork, changes.keys())
    for key, value in changes.items():
        setattr(artwork, key, value)
    session.flush()
    audit.record_diff(
        session,
        entity="card_artwork",
        entity_id=artwork_id,
        action="update",
        before=before,
        after={key: getattr(artwork, key) for key in changes},
    )
    return artwork


def delete_card_artwork(session: Session, artwork_id: int) -> None:
    artwork = session.get(CardArtwork, artwork_id)
    if artwork is None:
        raise NotFoundError("卡面不存在", entity="card_artwork", entity_id=artwork_id)
    if artwork.kind == "builtin":
        raise ProtectedEntityError("内置卡面不可删除", artwork_id=artwork_id, suggestion="choose_another")
    used = session.scalar(
        select(func.count(Account.id)).where(Account.card_style == artwork.key, Account.deleted_at.is_(None))
    )
    if used:
        raise ConflictError(
            f"该卡面已被 {int(used or 0)} 个账户使用，请先更换这些账户的卡面",
            artwork_id=artwork_id,
            account_count=int(used or 0),
        )
    session.delete(artwork)
    session.flush()
    audit.record(session, entity="card_artwork", entity_id=artwork_id, action="delete")
