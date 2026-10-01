"""审计留痕 —— 记录"谁在什么时候把哪个字段从什么改成了什么"。

设计取舍
--------
* **只记差异，不记快照**：整行快照会让审计表体积远超业务表，
  而排查时真正有用的恰恰是"哪个字段变了"。
* **不记录正文内容**：``note`` / ``payee`` 这类用户自由文本只记
  "长度从 N 变成 M"，避免审计表成为隐私副本。
* **不抛异常**：审计失败绝不能影响主业务 —— 它只应写进日志。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import AuditLog

__all__ = ["diff_fields", "record", "record_diff"]

_logger = logging.getLogger(__name__)

#: 这些字段只记录"是否变化"，不记录具体值（用户自由文本）
_REDACTED_FIELDS = frozenset({"note", "payee"})

#: 单次审计记录里最多保留的差异字段数（防止批量修改把 JSON 撑爆）
_MAX_DIFF_KEYS = 40


def diff_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """比较两次取值，返回 ``{字段: {"from":…, "to":…}}``。

    ``Decimal`` / ``datetime`` 等类型统一转成字符串，
    保证写进 JSON 列时不会因为不可序列化而失败。
    """
    changes: dict[str, Any] = {}
    for key in set(before) | set(after):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        if len(changes) >= _MAX_DIFF_KEYS:
            changes["…"] = "差异过多，已截断"
            break
        if key in _REDACTED_FIELDS:
            changes[key] = {"changed": True}
        else:
            changes[key] = {"from": _stringify(old), "to": _stringify(new)}
    return changes


def _stringify(value: Any) -> Any:
    """把值转换成可 JSON 序列化的形式；容器类型只记大小。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return f"<{type(value).__name__} len={len(value)}>"
    if isinstance(value, dict):
        return f"<dict keys={sorted(value)[:5]}>"
    return str(value)


def record(
    session: Session,
    *,
    entity: str,
    entity_id: int | None,
    action: str,
    changes: dict[str, Any] | None = None,
    actor: str = "user",
    note: str = "",
) -> AuditLog | None:
    """写入一条审计记录。

    返回创建的记录（便于测试断言），失败时返回 ``None`` 并记日志 ——
    审计是"附加价值"，它的失败不应该让用户的一笔记账失败。
    """
    try:
        entry = AuditLog(
            entity=entity,
            entity_id=entity_id,
            action=action,
            changes=changes or {},
            actor=actor,
            note=note,
        )
        session.add(entry)
        session.flush()
        return entry
    except Exception:
        _logger.exception("写入审计日志失败（已忽略）：entity=%s action=%s", entity, action)
        return None


def record_diff(
    session: Session,
    *,
    entity: str,
    entity_id: int | None,
    action: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor: str = "user",
    note: str = "",
) -> AuditLog | None:
    """比较前后取值并记录差异；无差异时不写记录。"""
    changes = diff_fields(before, after)
    if not changes:
        return None
    return record(
        session,
        entity=entity,
        entity_id=entity_id,
        action=action,
        changes=changes,
        actor=actor,
        note=note,
    )


def snapshot(model: Any, fields: Iterable[str]) -> dict[str, Any]:
    """从 ORM 实例提取指定字段的当前值，供后续比较。"""
    return {name: getattr(model, name, None) for name in fields}
