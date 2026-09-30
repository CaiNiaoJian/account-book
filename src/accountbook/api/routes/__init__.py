"""路由包。

按业务域拆分路由模块（P0 只有 ``system``；P1 起增加 transactions / accounts /
categories / reports / imports / db …）。每个模块只负责 **HTTP 契约与参数校验**，
真正的业务规则一律下沉到 ``accountbook.services``。
"""

from __future__ import annotations

__all__: list[str] = []
