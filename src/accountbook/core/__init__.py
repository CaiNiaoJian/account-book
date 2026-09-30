"""核心基础设施层。

本包只放**与界面、与具体业务无关**的东西：安全原语、单实例锁等。
P1 起会把领域模型（金额、分摊、周期）也纳入 ``core``，
届时 ``core`` 将成为可以被单元测试完全覆盖、且不 import 任何 IO 框架的纯净层。
"""

from __future__ import annotations

__all__: list[str] = []
