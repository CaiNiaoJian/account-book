"""FastAPI 依赖与异常映射。

分层边界
--------
* 服务层抛 :class:`~accountbook.core.errors.DomainError` 的子类；
* 本模块把它们翻译成 HTTP 状态码 + 结构化响应体；
* 路由函数只写「取参数 → 调服务 → 返回模型」，不含任何判断。

这样前端拿到的是稳定的 ``code`` 字段（如 ``conflict``、``validation_error``），
可以据此显示本地化文案，而不是把后端的中文句子直接摊在界面上 ——
后者会让界面语言与设置不一致。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from ..core.errors import DomainError
from .state import context_of

__all__ = ["get_session", "register_domain_error_handler"]

_logger = logging.getLogger(__name__)


def get_session(request: Request) -> Iterator[Session]:
    """每个请求一个数据库会话。

    FastAPI 的同步端点在线程池中执行，会话**不能**跨请求共享 ——
    共享会让两个并发请求互相看到对方未提交的中间状态。
    """
    ctx = context_of(request.app)
    database = ctx.database
    if database is None:  # pragma: no cover - 只会在启动流程被破坏时发生
        raise DomainError(
            "数据库尚未就绪",
        )

    with database.operation():
        yield from _session(database)


def _session(database) -> Iterator[Session]:
    session = database.new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def register_domain_error_handler(app: object) -> None:
    """注册领域异常 → HTTP 响应的统一处理器。"""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    if not isinstance(app, FastAPI):  # pragma: no cover - 编程错误
        raise TypeError("register_domain_error_handler 需要一个 FastAPI 实例")

    @app.exception_handler(DomainError)
    async def _handle(_request: Request, exc: DomainError) -> JSONResponse:
        # 4xx 属于"用户输入问题"，用 info 级；5xx 才是需要关注的异常
        if exc.http_status >= 500:  # pragma: no cover - 目前没有 5xx 类领域异常
            _logger.exception("领域异常（服务端）：%s", exc.message)
        else:
            _logger.info("领域异常：%s %s", exc.code, exc.message)
        return JSONResponse(exc.to_payload(), status_code=exc.http_status)
