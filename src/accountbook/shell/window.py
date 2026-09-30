"""桌面外壳实现 —— pywebview（Edge WebView2）与浏览器降级方案。

设计要点
--------
1. **外壳不含业务逻辑**：只负责开窗、加载本地 URL、把主题应用到原生装饰、
   保存窗口几何。所有数据操作都发生在前端 → HTTP → 服务层这条链路上。
2. **降级而非崩溃**：pywebview 依赖 pythonnet/CLR，在无桌面会话、
   受限沙箱或 CLR 不可用时无法启动。此时切换为
   :class:`BrowserShell`（用系统默认浏览器打开同一个 URL），
   功能完整，只是少了原生窗口外观。
3. **WebView2 用户数据目录必须持久化**（``private_mode=False`` +
   ``storage_path``）：否则 localStorage 每次启动都会清空，
   "记住用户选择的主题/语言"将无从实现。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import webbrowser
from abc import ABC, abstractmethod
from typing import Any

from ..api.state import AppContext
from ..config import RuntimeSettings
from . import dwm
from .clr_runtime import resolve_clr_runtime

__all__ = ["BrowserShell", "ShellAdapter", "WebViewShell", "create_shell"]

_logger = logging.getLogger(__name__)

#: 窗口背景色的兜底值：与前端设计令牌中的 ``--ab-bg`` 保持一致，
#: 用于 WebView 首次绘制前填充，避免深色主题下闪一下白屏。
_BACKGROUND = {"light": "#F5F5F7", "dark": "#1C1C1E"}


class ShellAdapter(ABC):
    """外壳协议：后端只依赖这四个方法（与 :class:`~accountbook.api.state.ShellBridge` 对应）。"""

    #: 外壳标识，用于「关于」页与日志
    kind: str = "abstract"

    def __init__(self, ctx: AppContext, settings: RuntimeSettings, url: str) -> None:
        self.ctx = ctx
        self.settings = settings
        self.url = url
        self._window: Any | None = None

    @abstractmethod
    def start(self) -> None:
        """阻塞运行外壳，直到用户关闭窗口。"""

    @abstractmethod
    def apply_theme(self, theme: str) -> None:
        """把主题应用到原生窗口装饰（``system`` 由实现自行解析）。"""

    @abstractmethod
    def show_window(self) -> None:
        """恢复并聚焦窗口。"""

    @abstractmethod
    def quit(self) -> None:
        """请求退出应用。"""

    # ---- 可选能力（默认空实现，子类按需覆盖） -------------------------------
    def hide_to_tray(self) -> None:
        """最小化到托盘（P0 未启用，P6 定时任务常驻时使用）。"""
        return None

    def notify(self, title: str, message: str) -> None:
        """发送系统通知（P6 起使用）。"""
        return None


class WebViewShell(ShellAdapter):
    """基于 pywebview + Edge WebView2 的原生窗口外壳。"""

    kind = "pywebview"

    def __init__(self, ctx: AppContext, settings: RuntimeSettings, url: str) -> None:
        super().__init__(ctx, settings, url)
        self._closing_saved = False

    # ---- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        """创建窗口并进入 GUI 事件循环（本方法会阻塞到窗口关闭）。"""
        self._warm_up_clr()

        import webview  # 延迟导入：让不需要外壳的场景（测试、纯 API）避免加载 CLR

        prefs = self.ctx.config.snapshot()
        effective = dwm.resolve_effective_theme(prefs.theme)
        window_state = prefs.window

        icon_path = self.ctx.paths.app_icon
        self._window = webview.create_window(
            title=f"{self.ctx.paths.data.name} · 记账本",  # 占位，前端加载后会通过 title API 更新
            url=self.url,
            width=window_state.width,
            height=window_state.height,
            x=window_state.x,
            y=window_state.y,
            maximized=window_state.maximized,
            min_size=(self.settings.window_min_width, self.settings.window_min_height),
            resizable=True,
            background_color=_BACKGROUND[effective],
            confirm_close=False,
            text_select=True,  # 允许选中复制金额/文本，否则用户会觉得"界面是死的"
            zoomable=False,  # 禁用 WebView 缩放，避免误触 Ctrl+滚轮导致布局错乱
            easy_drag=False,  # 使用原生边框，不需要"拖动整个客户区"
            frameless=False,  # 保留原生边框：贴靠、缩放、多屏移动行为最可靠
            vibrancy=False,  # Windows 上不支持 macOS 的 vibrancy
        )

        assert self._window is not None
        self._window.events.closing += self._on_closing
        self._window.events.closed += self._on_closed

        # 把外壳注册进上下文，后端接口随后即可反向操作窗口（主题同步、托盘唤起）
        self.ctx.bind_shell(self)

        gui = self.settings.gui_backend or None
        _logger.info(
            "启动桌面外壳：gui=%s debug=%s private_mode=False storage=%s",
            gui or "auto",
            self.settings.webview_debug,
            self.ctx.paths.webview_storage,
        )
        webview.start(
            func=self._on_gui_ready,
            gui=gui,
            debug=self.settings.webview_debug,
            private_mode=False,  # 必须为 False，否则前端偏好无法持久化
            storage_path=str(self.ctx.paths.webview_storage),
            icon=str(icon_path) if icon_path.exists() else None,
        )

    def _warm_up_clr(self) -> None:
        """预加载 pywebview 未声明的 CLR 引用。

        已知缺口：pywebview 的 winforms 后端会 ``from Microsoft.Win32 import SystemEvents``，
        但它只为 System.Windows.Forms / System.Collections / System.Threading /
        System.Reflection 调用了 ``clr.AddReference``，**漏了 SystemEvents**。

        * 在 **.NET Framework（netfx）** 下这不是问题：``Microsoft.Win32.SystemEvents``
          位于 System.dll，早已加载；
        * 在 **.NET Core（coreclr）** 下它是独立程序集，于是导入失败，
          pywebview 会抛出 "You must have pythonnet installed" —— 一个极具误导性的错误。

        这里在 ``webview.start()`` 之前主动补上引用。pythonnet 会缓存已加载的程序集，
        因此 pywebview 稍后的导入即可命中。

        失败时不抛异常：netfx 下这个引用名本就不存在，属于预期情况。
        """
        try:
            import clr
        except ImportError as exc:  # pragma: no cover - 运行时探测已排除该情况
            _logger.debug("CLR 不可用，跳过引用预热：%s", exc)
            return

        # 清单式声明，便于未来 pywebview 版本变化时继续补充；
        # 每项独立 try，避免一个失败影响其余项。
        for reference in ("Microsoft.Win32.SystemEvents",):
            try:
                clr.AddReference(reference)
                _logger.debug("已预加载 CLR 引用：%s", reference)
            except Exception as exc:  # noqa: BLE001 - 该引用在当前运行时可能本就不存在
                _logger.debug("预加载 CLR 引用 %s 失败（当前运行时可能不需要）：%s", reference, exc)

    def _on_gui_ready(self) -> None:
        """GUI 线程就绪回调：此时窗口句柄已存在，可以应用 DWM 美化。"""
        self._apply_window_style()
        self._sync_window_title()

    def _apply_window_style(self) -> None:
        """把当前主题应用到原生标题栏（覆盖 pywebview 基于系统主题的默认行为）。"""
        hwnd = self._hwnd()
        if not hwnd:
            _logger.debug("未能取得窗口句柄，跳过 DWM 美化")
            return
        prefs = self.ctx.config.snapshot()
        effective = dwm.resolve_effective_theme(prefs.theme)
        dwm.apply_window_style(
            hwnd,
            theme=effective,
            backdrop="none" if self.settings.window_backdrop == "auto" else self.settings.window_backdrop,
            rounded=True,
        )

    def _hwnd(self) -> int:
        """取得 Win32 窗口句柄。

        pywebview 的 ``window.native`` 是 WinForms 的 ``BrowserForm``；
        访问时机太早（窗口尚未创建）会抛异常，因此统一在此做保护。
        """
        window = self._window
        if window is None:
            return 0
        try:
            native = getattr(window, "native", None)
            if native is None:
                return 0
            return int(native.Handle.ToInt32())
        except Exception as exc:  # noqa: BLE001 - 句柄不可得时仅退化外观
            _logger.debug("读取窗口句柄失败：%s", exc)
            return 0

    def _sync_window_title(self) -> None:
        """设置窗口标题（含版本号，便于用户区分多开的版本）。"""
        window = self._window
        if window is None:
            return
        try:
            from .. import APP_NAME, __version__

            window.title = f"{APP_NAME} {__version__}"
        except Exception as exc:  # noqa: BLE001
            _logger.debug("设置窗口标题失败：%s", exc)

    # ---- 窗口几何持久化 -----------------------------------------------------
    def _on_closing(self) -> None:
        """关闭前保存窗口几何。

        只在 closing 时保存一次（而不是监听每次 resize）：
        拖动窗口会触发大量事件，频繁写 config.json 既无必要也增加损坏风险。
        """
        if self._closing_saved:
            return
        self._closing_saved = True
        window = self._window
        if window is None:
            return
        try:
            self.ctx.config.set_window_state(
                width=int(window.width),
                height=int(window.height),
                x=int(window.x),
                y=int(window.y),
                maximized=bool(getattr(window, "maximized", False)),
            )
            _logger.debug("已保存窗口几何：%sx%s", window.width, window.height)
        except Exception as exc:  # noqa: BLE001 - 保存失败不影响退出
            _logger.debug("保存窗口几何失败：%s", exc)

    def _on_closed(self) -> None:
        """窗口已关闭：通知后端收敛。"""
        _logger.info("窗口已关闭，请求后端停机")
        self.ctx.shutdown_event.set()

    # ---- ShellBridge 实现 ---------------------------------------------------
    def apply_theme(self, theme: str) -> None:
        """主题变更后同步原生标题栏。

        可能由 uvicorn 工作线程调用；DWM 接口接受任意线程传入的 HWND，
        因此无需切回 GUI 线程。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return
        effective = dwm.resolve_effective_theme(theme)
        dwm.set_dark_mode(hwnd, effective == "dark")
        _logger.debug("标题栏主题已同步：%s", effective)

    def show_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.show()
            window.restore()
            window.on_top = True
            window.on_top = False
        except Exception as exc:  # noqa: BLE001
            _logger.debug("唤起窗口失败：%s", exc)

    def hide_to_tray(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.hide()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("隐藏窗口失败：%s", exc)

    def quit(self) -> None:
        window = self._window
        if window is None:
            self.ctx.shutdown_event.set()
            return
        try:
            window.destroy()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("销毁窗口失败，改为设置停机信号：%s", exc)
            self.ctx.shutdown_event.set()


class BrowserShell(ShellAdapter):
    """降级外壳：用系统默认浏览器打开同一个本地服务地址。

    适用场景：
        * pywebview / pythonnet 不可用（受限环境、缺少 WebView2 运行时）；
        * Linux/macOS 上尚未适配原生外壳时；
        * CI 中做端到端检查。

    功能与原生外壳**完全等价**（同一套前端、同一套 API、同样的令牌），
    差别仅在于没有原生窗口装饰，且关闭浏览器标签时会话需要手动结束。
    """

    kind = "browser"

    def start(self) -> None:
        _logger.warning(
            "已降级为浏览器外壳（未使用原生窗口）。若这不是预期行为，请检查 pywebview / WebView2 运行时。"
        )
        try:
            webbrowser.open(self.url, new=1, autoraise=True)
        except Exception as exc:  # noqa: BLE001
            _logger.error("打开浏览器失败，请手动访问：%s（原因：%s）", self.url, exc)
        else:
            _logger.info("已在默认浏览器中打开：%s", self.url)

        # 阻塞主线程，直到收到停机信号（托盘退出 / Ctrl+C / 外部请求）
        try:
            while not self.ctx.shutdown_event.wait(0.5):
                pass
        except KeyboardInterrupt:  # pragma: no cover - 交互式中断
            _logger.info("收到中断信号，准备退出")

    def apply_theme(self, theme: str) -> None:
        """浏览器外壳无法影响浏览器自身的标题栏，仅记录。"""
        _logger.debug("浏览器外壳忽略原生主题同步：%s", theme)

    def show_window(self) -> None:
        try:
            webbrowser.open(self.url, new=0, autoraise=True)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("唤起浏览器失败：%s", exc)

    def quit(self) -> None:
        self.ctx.shutdown_event.set()


def create_shell(ctx: AppContext, settings: RuntimeSettings, url: str) -> ShellAdapter:
    """构造可用的外壳实现。

    选择逻辑（**先探测、后构造**）：
        1. 显式要求浏览器外壳（``ACCOUNTBOOK_SHELL=browser``）→ 直接用；
        2. 非 Windows 平台在 P0 阶段一律降级（原生外壳尚未适配）；
        3. 探测可用的 .NET 运行时：pythonnet 的 netfx 宿主在部分机器上无法解析
           CLR 类型（本机实测即如此），coreclr 正常。一个都不可用时**此时**就降级，
           而不是等到开窗那一刻才崩溃（详见 :mod:`accountbook.shell.clr_runtime`）；
        4. 否则尝试 import pywebview：失败则降级。
    """
    import os

    forced = os.environ.get("ACCOUNTBOOK_SHELL", "").strip().lower()
    if forced == "browser":
        return BrowserShell(ctx, settings, url)

    if sys.platform != "win32":
        _logger.info("当前平台非 Windows，P0 阶段使用浏览器外壳")
        return BrowserShell(ctx, settings, url)

    # 必须在导入 pywebview 的 Windows 后端（进而 import clr）之前完成运行时选择
    runtime = resolve_clr_runtime(ctx.paths)
    ctx.clr_runtime = runtime
    if runtime is None:
        return BrowserShell(ctx, settings, url)

    try:
        import webview  # noqa: F401  （仅探测可用性）
    except Exception as exc:  # noqa: BLE001 - 探测失败即降级
        _logger.warning("pywebview 不可用（%s），降级为浏览器外壳", exc)
        return BrowserShell(ctx, settings, url)

    return WebViewShell(ctx, settings, url)


def start_background_watchdog(ctx: AppContext, shell: ShellAdapter) -> threading.Thread:  # pragma: no cover
    """启动看门狗线程：外壳意外退出时确保后端也收敛。

    P0 暂未启用（保留给 P6 定时任务常驻场景）。
    """

    def _watch() -> None:
        while not ctx.shutdown_event.is_set():
            time.sleep(0.5)
        shell.quit()

    thread = threading.Thread(target=_watch, name="shell-watchdog", daemon=True)
    thread.start()
    return thread
