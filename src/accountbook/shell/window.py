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

from .. import APP_NAME_EN, APP_WINDOW_TITLE, __version__
from ..api.state import AppContext
from ..config import RuntimeSettings
from ..core.security import mask_token_in_url
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
            # 标题即"应用标记 + 版本号"：标记与前端 document.title 一致，
            # 浏览器外壳据此确认窗口是否真的出现（见 BrowserShell._wait_for_app_window）
            title=f"{APP_WINDOW_TITLE} {__version__}",
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
        """设置窗口标题。

        标题 = 应用标记 + 版本号。标记部分必须与
        :data:`accountbook.APP_WINDOW_TITLE` 完全一致（浏览器外壳靠它确认窗口是否出现）。
        """
        window = self._window
        if window is None:
            return
        try:
            window.title = f"{APP_WINDOW_TITLE} {__version__}"
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

    ⚠️ 核心约束：**绝不能让用户面对一个看不见的进程**
    ---------------------------------------------------
    这是实际踩过的坑：原生窗口不可用 → 降级到浏览器 → 但浏览器没能打开，
    而托盘图标又恰好创建失败，于是用户双击程序后"什么都不发生"，
    任务管理器里却躺着一个常驻进程。这比直接报错糟糕得多。

    因此本实现遵守两条规则：
        1. **依次尝试三种打开方式**，并且**检查返回值**（旧实现无论成败都记"已打开"）；
        2. 全部失败时**必须给用户一个可见出口**：弹窗给出可复制的地址，
           并提供「重试 / 退出」，而不是继续静默运行。
    """

    kind = "browser"

    def start(self) -> None:
        _logger.warning(
            "已降级为浏览器外壳（未使用原生窗口）。若这不是预期行为，请检查 pywebview / WebView2 运行时。"
        )

        # 注意：``webbrowser.open`` **会谎报成功** ——
        # 它在 Windows 上基于 ``os.startfile``，只要 URL 关联存在就返回 True，
        # 即便浏览器进程根本没起来（实测：返回 True 但没有任何浏览器进程）。
        # 因此这里必须**确认真实窗口出现**，而不能相信返回值。
        opened = self._open_browser(self.url)
        if opened and self._wait_for_app_window():
            _logger.info("已在默认浏览器中打开界面：%s", _mask_url(self.url))
        else:
            _logger.error(
                "未能确认浏览器窗口出现（open 返回值=%s），改为向用户提示手动访问：%s",
                opened,
                _mask_url(self.url),
            )
            if not self._prompt_until_visible():
                _logger.info("用户选择退出，停止本地服务")
                self.ctx.shutdown_event.set()
                return

        # 阻塞主线程，直到收到停机信号（托盘退出 / Ctrl+C / 外部请求）
        try:
            while not self.ctx.shutdown_event.wait(0.5):
                pass
        except KeyboardInterrupt:  # pragma: no cover - 交互式中断
            _logger.info("收到中断信号，准备退出")

    # ---- 打开浏览器：三种方式 + 返回值校验 ----------------------------------
    @staticmethod
    def _open_browser(url: str) -> bool:
        """尝试打开默认浏览器，返回调用方是否**声称**成功。

        三种方式按"尊重用户设置 → 直接调用外壳 → 兜底命令"排序：
            1. ``webbrowser``：遵循系统默认浏览器设置（正常情况下首选）；
            2. ``os.startfile``：Windows Shell 关联，绕过 webbrowser 的注册表查找；
            3. ``cmd /c start``：最后手段，覆盖 Shell 关联被破坏的情况。

        ⚠️ 返回值只代表"调用没有报错"，**不代表窗口真的出现了**。
        调用方必须再用 :meth:`_wait_for_app_window` 确认（见 ``start``）。
        """
        import os
        import subprocess

        # 1) 标准库：会读取用户配置的默认浏览器
        try:
            if webbrowser.open(url, new=1, autoraise=True):
                return True
            _logger.debug("webbrowser.open 返回 False，尝试下一种方式")
        except Exception as exc:  # noqa: BLE001 - 任何异常都只意味着"这条路不通"
            _logger.debug("webbrowser.open 失败：%s", exc)

        # 2) Windows Shell 关联（成功不抛异常即视为成功）
        if sys.platform == "win32":
            try:
                # URL 完全由本进程构造（环回地址 + 本地令牌），不含任何外部输入
                os.startfile(url)
                return True
            except OSError as exc:
                _logger.debug("os.startfile 失败：%s", exc)

        # 3) 命令行兜底
        try:
            # 参数固定为 cmd 的内置 start 用法，URL 由本进程构造
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError as exc:
            _logger.debug("cmd start 失败：%s", exc)

        return False

    # ---- 确认窗口真的出现 ---------------------------------------------------
    @staticmethod
    def _visible_window_titles() -> list[str]:
        """枚举当前可见的顶层窗口标题。

        只用标准库 ctypes 调用 ``EnumWindows``：
        引入 psutil 这类依赖只为读窗口标题并不划算，而窗口标题恰好是
        "我的界面到底出来了没有"最可靠的信号（浏览器标签标题即页面 ``<title>``）。
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        titles: list[str] = []
        # 回调必须保持引用直到 EnumWindows 返回，否则会被 GC 提前回收
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _collect(hwnd: int, _param: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value:
                titles.append(buffer.value)
            return True

        try:
            user32.EnumWindows(callback_type(_collect), 0)
        except OSError as exc:  # pragma: no cover - 极端环境
            _logger.debug("枚举窗口失败：%s", exc)
        return titles

    @classmethod
    def _wait_for_app_window(cls, timeout: float = 4.0) -> bool:
        """在超时内轮询"标题里带应用标记"的可见窗口。

        判定依据是 :data:`accountbook.APP_WINDOW_TITLE`（``记账本 · AccountBook``）：
        它是前端 ``document.title``（浏览器标签即页面标题）与原生窗口标题共同的标记。

        为什么不用宽松的"任意浏览器窗口"或单独的应用名：
        实测中另一个应用的窗口标题里恰好含"记账本"，
        宽松判定会把"其实没打开"误判为成功 —— 而这正是我们要根除的失败模式。
        """
        if sys.platform != "win32":
            return True  # 非 Windows 无法枚举，保守认为成功（上层仍会记录日志）

        deadline = time.monotonic() + timeout
        while True:
            for title in cls._visible_window_titles():
                if APP_WINDOW_TITLE in title:
                    _logger.debug("已确认界面窗口出现：%s", title)
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _prompt_until_visible(self) -> bool:
        """可见出口：提示用户手动访问，并在用户要求时反复重试。

        返回是否应当继续运行。用户选择退出时返回 ``False`` ——
        绝不在用户不知情的情况下继续当一个看不见的进程。
        """
        if sys.platform != "win32":
            _logger.warning("请手动在浏览器中打开：%s", self.url)
            return True

        while True:
            result = self._show_prompt()
            if result is None:
                # 弹窗本身失败：保守地继续运行，但已在日志里留下地址
                _logger.warning("提示窗口不可用，请手动访问：%s", self.url)
                return True
            if result != 4:  # 非 IDRETRY ⇒ 用户选择退出
                return False
            self._open_browser(self.url)
            if self._wait_for_app_window(timeout=5.0):
                _logger.info("重试成功，界面已打开")
                return True

    def _show_prompt(self) -> int | None:
        """弹出一个可复制的地址提示框；返回 Windows 对话框按钮值。"""
        message = (
            "本机无法使用原生窗口，界面已尝试在系统浏览器中打开。\n\n"
            "如果没有看到界面，请把下面的地址复制到浏览器中打开：\n\n"
            f"{self.url}\n\n"
            "· 「重试」：再次尝试自动打开浏览器；\n"
            "· 「取消」：退出程序（不会在后台保留进程）。\n\n"
            "提示：本地服务已经就绪，地址一旦被打开即可正常使用。"
        )
        try:
            import ctypes

            # MB_ICONINFORMATION(0x40) | MB_RETRYCANCEL(0x05) | MB_SETFOREGROUND | MB_TOPMOST
            return int(
                ctypes.windll.user32.MessageBoxW(
                    None, message, f"{APP_NAME_EN} · 请在浏览器中打开界面", 0x40 | 0x05 | 0x10000 | 0x40000
                )
            )
        except Exception as exc:  # noqa: BLE001 - 弹窗失败时由调用方兜底
            _logger.debug("提示窗口创建失败：%s", exc)
            return None

    def apply_theme(self, theme: str) -> None:
        """浏览器外壳无法影响浏览器自身的标题栏，仅记录。"""
        _logger.debug("浏览器外壳忽略原生主题同步：%s", theme)

    def show_window(self) -> None:
        if not self._open_browser(self.url):
            _logger.warning("无法唤起浏览器，请手动访问：%s", self.url)

    def quit(self) -> None:
        self.ctx.shutdown_event.set()


def _mask_url(url: str) -> str:
    """日志中脱敏入口地址里的令牌（与 app.py 共用同一实现）。"""
    return mask_token_in_url(url)


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
