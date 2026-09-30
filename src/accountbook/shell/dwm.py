"""Windows 桌面窗口合成（DWM）美化 —— Apple 质感的第一步（REQ-12）。

为什么需要这个模块
------------------
pywebview 在 Windows 上用的是 WinForms 窗口，外观是"经典 Windows"：
直角、浅色标题栏、系统边框色。这与项目「借鉴 Apple 美术风格」的定位冲突。

Windows 11 提供了 DWM（Desktop Window Manager）属性接口，可以在**不改窗口结构**
的前提下做到：

============================  ==================================================
DWMWA 属性                     效果
============================  ==================================================
20 ``IMMERSIVE_DARK_MODE``    标题栏切换深色（跟随系统主题或应用主题）
33 ``WINDOW_CORNER_PREFERENCE`` 窗口圆角（Apple 风格的关键一笔）
34 ``BORDER_COLOR``           窗口边框颜色（可设为极淡，弱化系统感）
35 ``CAPTION_COLOR``          标题栏底色（可与应用工具栏同色，形成"统一工具栏"）
38 ``SYSTEMBACKDROP_TYPE``    系统背景材质（Mica / Acrylic）
============================  ==================================================

**谨慎原则**：这些 API 只在 Windows 10 1809+ 存在，且部分属性要 Windows 11 22H2+
才生效。全部调用都包在 try/except 中，失败即静默降级为系统默认外观——
绝不允许"美化失败导致程序打不开"。

pywebview 自身也会设置属性 20/38（``update_title_bar_theme``），但它依据的是
**系统**主题；当用户在应用内把主题切成深色而系统仍是浅色时，标题栏会不匹配。
因此本模块在每次主题变更后**覆盖**设置，以应用主题为准。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Literal

__all__ = [
    "BackdropKind",
    "ThemeName",
    "apply_window_style",
    "enable_dpi_awareness",
    "get_windows_build",
    "set_backdrop",
    "set_border_color",
    "set_caption_color",
    "set_dark_mode",
    "set_rounded_corners",
    "supports_backdrop",
]

_logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# -----------------------------------------------------------------------------
# DWMWINDOWATTRIBUTE 常量
# 参考资料：Microsoft Learn · DwmSetWindowAttribute
# -----------------------------------------------------------------------------
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_SYSTEMBACKDROP_TYPE = 38

# DWM_WINDOW_CORNER_PREFERENCE
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

# DWM_SYSTEMBACKDROP_TYPE
DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2  # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic
DWMSBT_TABBEDWINDOW = 4  # Tabbed（Mica Alt）

#: 表示"使用系统默认颜色"的哨兵值（DWM 文档：0xFFFFFFFE = 默认，0xFFFFFFFF = 无）
DWM_COLOR_DEFAULT = 0xFFFFFFFE
DWM_COLOR_NONE = 0xFFFFFFFF

ThemeName = Literal["light", "dark"]
BackdropKind = Literal["auto", "mica", "acrylic", "tabbed", "none"]

#: Windows 11 22H2（build 22621）才支持 SYSTEMBACKDROP_TYPE
_MIN_BUILD_FOR_BACKDROP = 22621


# -----------------------------------------------------------------------------
# 底层绑定
# -----------------------------------------------------------------------------
def _dwm_set(hwnd: int, attribute: int, value: int) -> bool:
    """调用 ``DwmSetWindowAttribute``。

    返回是否成功。**任何失败都只记 debug 日志**：
    在 Windows 10 或远程桌面会话中，这些属性大多不受支持，
    属于预期情况，不应污染用户日志。
    """
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        size = ctypes.sizeof(ctypes.c_int)
        result = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(attribute),
            ctypes.byref(ctypes.c_int(value)),
            ctypes.c_uint(size),
        )
    except (OSError, AttributeError) as exc:  # pragma: no cover - 依赖具体系统
        _logger.debug("DWM 属性 %s 设置失败：%s", attribute, exc)
        return False
    if result != 0:
        _logger.debug("DWM 属性 %s 返回错误码 %s（该系统可能不支持）", attribute, result)
        return False
    return True


def _rgb_to_colorref(red: int, green: int, blue: int) -> int:
    """把 RGB 三元组转成 Windows 的 COLORREF（0x00BBGGRR）。

    这是 Windows API 的经典陷阱：COLORREF 的字节序与直觉相反。
    在这里集中转换，避免每个调用点各写一遍。
    """
    return (blue << 16) | (green << 8) | red


def get_windows_build() -> int:
    """取当前 Windows 内部版本号（非 Windows 返回 0）。

    用途：决定是否尝试 Mica 等 Windows 11 专属特性，
    避免在不支持的版本上反复失败并留下噪声日志。
    """
    if not IS_WINDOWS:
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:  # noqa: BLE001 - 属性缺失时按最保守处理
        return 0


def supports_backdrop() -> bool:
    """当前系统是否支持 ``SYSTEMBACKDROP_TYPE``（Mica / Acrylic）。"""
    return IS_WINDOWS and get_windows_build() >= _MIN_BUILD_FOR_BACKDROP


# -----------------------------------------------------------------------------
# 对外能力
# -----------------------------------------------------------------------------
def set_dark_mode(hwnd: int, dark: bool) -> bool:
    """设置标题栏与窗口边框的深色模式（跟随**应用**主题，而非系统主题）。"""
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def set_rounded_corners(hwnd: int, enabled: bool = True, small: bool = False) -> bool:
    """设置窗口圆角。

    Apple 风格窗口的关键特征之一是连续圆角。
    Windows 11 上默认就是圆角，但 WinForms 窗口有时会被绘制成直角，
    显式设置可保证一致性。
    """
    preference = (
        DWMWCP_ROUNDSMALL if (enabled and small) else (DWMWCP_ROUND if enabled else DWMWCP_DONOTROUND)
    )
    return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, preference)


def set_border_color(hwnd: int, rgb: tuple[int, int, int] | None = None) -> bool:
    """设置窗口边框颜色；``None`` 表示恢复系统默认。"""
    value = DWM_COLOR_DEFAULT if rgb is None else _rgb_to_colorref(*rgb)
    return _dwm_set(hwnd, DWMWA_BORDER_COLOR, value)


def set_caption_color(hwnd: int, rgb: tuple[int, int, int] | None = None) -> bool:
    """设置标题栏底色。

    P0 默认不调用：深色模式属性（20）已经能让标题栏与系统风格一致，
    手工染色在窗口激活/非激活状态切换时容易出现不协调。
    预留此接口供 P2「统一工具栏」视觉打磨使用。
    """
    value = DWM_COLOR_DEFAULT if rgb is None else _rgb_to_colorref(*rgb)
    return _dwm_set(hwnd, DWMWA_CAPTION_COLOR, value)


def set_backdrop(hwnd: int, kind: BackdropKind = "auto") -> bool:
    """设置系统背景材质。

    **重要现实**：WebView2 会不透明地铺满整个客户区，
    因此 Mica/Acrylic 在客户区看不见，只在窗口圆角边缘略有体现。
    这里把它当作"锦上添花"，默认使用 ``none`` 以保证绘制可预测、
    避免切主题时出现一次明显的重绘闪烁。
    """
    if not supports_backdrop():
        return False
    mapping: dict[str, int] = {
        "auto": DWMSBT_NONE,
        "none": DWMSBT_NONE,
        "mica": DWMSBT_MAINWINDOW,
        "acrylic": DWMSBT_TRANSIENTWINDOW,
        "tabbed": DWMSBT_TABBEDWINDOW,
    }
    return _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, mapping.get(kind, DWMSBT_NONE))


def apply_window_style(
    hwnd: int,
    *,
    theme: ThemeName,
    backdrop: BackdropKind = "none",
    rounded: bool = True,
    border_rgb: tuple[int, int, int] | None = None,
) -> dict[str, bool]:
    """一次性应用全部窗口美化，返回各项结果（供日志与诊断）。

    参数
    ----
    hwnd:
        Win32 窗口句柄（``window.native.Handle.ToInt32()``）。
    theme:
        应用当前**实际生效**的主题（``system`` 需由调用方先解析为 light/dark）。
    backdrop:
        背景材质，默认 ``none``（见 :func:`set_backdrop` 的说明）。
    rounded:
        是否启用圆角。
    border_rgb:
        自定义边框色；``None`` 用系统默认。
    """
    if not IS_WINDOWS or not hwnd:
        return {"supported": False}

    results = {
        "dark_mode": set_dark_mode(hwnd, theme == "dark"),
        "rounded": set_rounded_corners(hwnd, rounded),
        "border": set_border_color(hwnd, border_rgb),
        "backdrop": set_backdrop(hwnd, backdrop),
    }
    _logger.debug("窗口美化结果：%s", results)
    return results


def enable_dpi_awareness() -> bool:
    """开启 Per-Monitor V2 DPI 感知（REQ-7「全局兼容」）。

    为什么必须：不声明 DPI 感知时，Windows 会对窗口做位图拉伸，
    高分屏上字体与图标会发虚——对"美术格外重要"的项目是不可接受的。

    必须在**创建任何窗口之前**调用。若进程已被设置过（pywebview 或
    pythonnet 可能已设置），调用会返回失败，这属于正常情况，静默忽略。
    """
    if not IS_WINDOWS:
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
        if not ok:
            _logger.debug("DPI 感知已由运行时设置或不受支持（忽略）")
        return ok
    except (OSError, AttributeError):  # pragma: no cover - 老系统
        return False


def resolve_effective_theme(theme: str) -> ThemeName:
    """把 ``light|dark|system`` 解析为实际生效的 ``light|dark``。

    ``system`` 的判定读取注册表 ``AppsUseLightTheme``，
    与 pywebview 内部实现保持一致，避免"标题栏与应用主题不一致"。
    """
    normalized = (theme or "system").lower()
    if normalized in {"light", "dark"}:
        return normalized  # type: ignore[return-value]
    if not IS_WINDOWS:
        return "light"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) == 1 else "dark"
    except (OSError, ValueError, ImportError):
        return "light"
