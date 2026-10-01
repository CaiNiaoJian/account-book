"""应用上下文 —— 后端服务与桌面外壳共享的运行时状态容器。

为什么需要它
------------
FastAPI 的依赖注入需要一个"从应用实例取到全局状态"的载体。
若各处直接 import 全局单例，测试将无法注入临时目录与假端口；
若到处传参，路由签名会迅速膨胀。因此这里用**一个不可变数据类**
装下所有共享状态，通过 ``request.app.state.ctx`` 访问。

注意：``AppContext`` 本身是不可变的（frozen），但内部持有的
:class:`~accountbook.config.ConfigStore` 与令牌是可变的运行时对象，
这正是我们想要的语义——"容器不变，内容演进"。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..config import ConfigStore, RuntimeSettings
from ..core.security import SessionToken
from ..paths import AppPaths

__all__ = ["AppContext", "ShellBridge", "create_context"]


class ShellBridge(Protocol):
    """桌面外壳需要向后端暴露的最小能力集合。

    后端只通过这些方法影响窗口，**不直接 import pywebview**。
    这样做的收益：
        * 后端可以脱离图形环境跑单元测试；
        * 未来替换外壳（Qt / 纯浏览器 / 移动端壳）时无需改后端；
        * 打包时若 pywebview 不可用，后端仍能独立启动（降级为浏览器模式）。
    """

    def apply_theme(self, theme: str) -> None:
        """把主题（light/dark/system）应用到原生窗口装饰。"""
        ...

    def show_window(self) -> None:
        """从托盘/最小化状态恢复并聚焦窗口。"""
        ...

    def quit(self) -> None:
        """请求退出应用。"""
        ...


@dataclass(slots=True)
class AppContext:
    """一次进程生命周期内的共享状态。"""

    paths: AppPaths
    settings: RuntimeSettings
    config: ConfigStore
    token: SessionToken
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    port: int = 0
    shell: ShellBridge | None = None

    #: 实际生效的外壳类型（``pywebview`` / ``browser``）—— 由 bind_shell 填入。
    #: 之所以要暴露给界面：当原生窗口因环境原因不可用而自动降级时，
    #: 用户必须能**看到**这件事与原因，否则只会觉得"这软件怎么没有窗口"。
    shell_kind: str = "unknown"

    #: 选定的 .NET 运行时（``netfx`` / ``coreclr`` / ``None``）
    clr_runtime: str | None = None

    #: 数据库实例。由 app.run() 在迁移完成后注入；
    #: 为 ``None`` 时任何数据接口都应返回 503，而不是抛出难以理解的 AttributeError。
    database: Any = None

    #: 后端线程退出信号。uvicorn 通过它优雅停机。
    shutdown_event: threading.Event = field(default_factory=threading.Event)

    @property
    def origin_whitelist(self) -> frozenset[str]:
        """当前端口的合法 Origin 集合（端口确定后才正确）。"""
        from ..core.security import build_origin_whitelist

        return build_origin_whitelist(self.port)

    def uptime_seconds(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def bind_shell(self, shell: ShellBridge) -> None:
        """在外壳创建完成后回填引用（端口 → 建窗 → 回填，存在先后依赖）。"""
        self.shell = shell
        self.shell_kind = getattr(shell, "kind", "unknown")


def create_context(
    *,
    paths: AppPaths,
    settings: RuntimeSettings,
    config: ConfigStore,
) -> AppContext:
    """构造上下文并生成一次性会话令牌。"""
    return AppContext(
        paths=paths,
        settings=settings,
        config=config,
        token=SessionToken.generate(),
    )


def context_of(app: Any) -> AppContext:
    """从 FastAPI 应用实例取回上下文（供路由与中间件使用）。"""
    ctx = getattr(app.state, "ctx", None)
    if not isinstance(ctx, AppContext):  # pragma: no cover - 属于编程错误
        raise RuntimeError("应用上下文未初始化：请通过 create_app(ctx) 构造应用")
    return ctx
