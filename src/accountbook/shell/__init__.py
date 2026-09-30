"""桌面外壳适配层（Shell）。

本包把「窗口系统」与「应用逻辑」彻底隔开：

    app.py ──调用──▶ ShellAdapter（协议）
                        ├── WebViewShell  ← pywebview + Edge WebView2（当前实现）
                        └── (未来) QtShell / BrowserShell / HeadlessShell

之所以要这层抽象，是因为 pywebview 依赖 pythonnet 与 CLR，
在某些受限环境（无桌面会话、沙箱、CI）下无法初始化。
有了适配层，后端服务可以独立启动，外壳换成"打开系统浏览器"即可继续工作，
而不是整个应用起不来。
"""

from __future__ import annotations

__all__: list[str] = []
