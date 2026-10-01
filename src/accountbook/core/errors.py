"""领域异常 —— 让"哪种失败"成为可判别的事实，而不是靠解析错误文本。

分层约定
--------
* ``core`` / ``services`` 只抛本模块定义的异常，**不抛 HTTPException** ——
  领域层不该知道 HTTP 的存在；
* ``api`` 层用一个统一异常处理器把领域异常映射成状态码与结构化响应；
* 前端据此得到稳定的 ``code`` 字段，可以据此显示本地化文案，
  而不是把后端返回的中文句子直接摊在界面上。

需求追溯：REQ-3（功能可靠）、REQ-10（可维护）
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ConflictError",
    "DomainError",
    "NotFoundError",
    "ProtectedEntityError",
    "ValidationError",
]


class DomainError(Exception):
    """领域异常基类。

    ``code`` 是**稳定的机器可读标识**（不随文案变化），
    ``details`` 携带定位问题所需的结构化信息（例如冲突字段、余额缺口）。
    """

    #: 子类覆盖；会出现在 API 响应的 ``code`` 字段
    code: str = "domain_error"
    #: 默认 HTTP 状态码（仅 api 层使用；领域层自身不关心）
    http_status: int = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_payload(self) -> dict[str, Any]:
        """转换为 API 响应体。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(DomainError):
    """输入不合法（业务规则层面，而非类型层面）。

    例如：转账缺少目标账户、分账金额之和不等于流水金额、日期超出允许范围。
    """

    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    """目标不存在（或已被软删除）。"""

    code = "not_found"
    http_status = 404

    def __init__(self, message: str, *, entity: str = "", entity_id: Any = None, **details: Any) -> None:
        super().__init__(message, entity=entity, entity_id=entity_id, **details)


class ConflictError(DomainError):
    """与现有数据冲突（例如同名账户、分类下仍有子分类）。"""

    code = "conflict"
    http_status = 409


class ProtectedEntityError(DomainError):
    """受保护的内置数据不可删除（但允许改名/改图标）。

    设计立场：内置分类是"开箱可用"的基础，误删会让新用户无所适从；
    但把它们做成完全不可改又会显得僵硬。折中方案是**可改不可删**，
    并给出明确的替代动作（隐藏 / 重命名 / 合并）。
    """

    code = "protected_entity"
    http_status = 409
