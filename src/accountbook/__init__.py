"""AccountBook Desktop —— 本地优先、离线可用、Apple 美学的个人记账本桌面工具。

包结构（详细职责见 docs/ARCHITECTURE.md）::

    accountbook
    ├── __main__.py     进程入口：参数解析 → app.run()
    ├── app.py          应用编排：日志 → 单实例 → 后端服务 → 桌面外壳 → 生命周期
    ├── config.py       应用配置（环境变量 + 可持久化 JSON 配置）
    ├── paths.py        路径解析：安装版 / 便携版 双模式数据目录
    ├── logging_setup.py 结构化日志、轮转、未捕获异常兜底
    ├── core/           与界面无关的纯业务基础设施（安全、单实例锁）
    ├── api/            FastAPI 同源服务（REST + SSE），仅绑定 127.0.0.1
    ├── shell/          桌面外壳适配层（pywebview / DWM 美化 / 托盘）
    └── resources/      随包分发的静态资源（字体、图标、主题、前端构建产物）

设计约束（贯穿全项目）：
    1. **业务逻辑不依赖界面**：shell 层只负责开窗与加载 URL，可整体替换。
    2. **不上云**：无遥测、无启动联网；AI 与更新检查必须由用户显式触发。
    3. **金额一律整数最小单位**（分），禁止浮点参与金额运算。
    4. 所有跨层调用通过显式接口，禁止 shell/api 直接操作数据库。
"""

from __future__ import annotations

__all__ = ["APP_ID", "APP_NAME", "APP_NAME_EN", "APP_WINDOW_TITLE", "BUILD_PHASE", "__version__"]

#: 版本号 —— 单一事实来源。打包脚本与「关于」页均从此处读取。
#: 版本规则：主.次.修订（主=不兼容的存储结构变更，次=功能阶段，修订=修复）
__version__ = "0.2.1"

#: 展示名（中文为主，需求 4 项取舍：中文优先 + i18n 预留英文）
APP_NAME = "记账本"
APP_NAME_EN = "AccountBook"

#: 应用标识 —— 同时作为 %LOCALAPPDATA% 下的目录名与单实例锁名
APP_ID = "AccountBook"

#: 窗口/页面标题的**稳定标记**（``记账本 · AccountBook``）。
#:
#: 它不只是好看：浏览器外壳需要靠它判断"界面窗口到底出来了没有"。
#: 用带间隔号的完整串而不是单独的中文名，是因为后者会误判 ——
#: 实测中另一个应用的窗口标题里恰好含"记账本"，导致把"没打开"误判为"已打开"。
#: 因此这个常量同时被前端 ``document.title``、原生窗口标题与窗口探测逻辑使用，
#: 三者必须始终一致。
APP_WINDOW_TITLE = f"{APP_NAME} · {APP_NAME_EN}"

#: 当前实现阶段 —— 前端用它在侧边栏为未实现功能打「计划于 Pn」标记
#: 需求 10：明确区分「已实现 / 已预留」，避免用户以为功能缺失是缺陷
BUILD_PHASE = "P6"
