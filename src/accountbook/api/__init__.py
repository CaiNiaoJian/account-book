"""HTTP 接口层（FastAPI）。

本包对外只暴露一个环回地址上的同源服务，供：
    * 桌面外壳中的前端页面调用（生产模式）；
    * 未来被外部自动化脚本 / 其它语言客户端调用（P8+ 的「API 接入」需求）。

安全边界全部在 :mod:`accountbook.core.security` 中定义，
:mod:`accountbook.api.server` 负责把它们**真正挂到每个请求上**。
"""

from __future__ import annotations

__all__: list[str] = []
