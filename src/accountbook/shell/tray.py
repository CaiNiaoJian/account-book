"""系统托盘（可选能力，缺失即静默降级）。

为什么 P0 就要有托盘
--------------------
虽然 P0 还没有定时任务，但下面两件事从第一天起就需要一个"窗口之外还有我"的落点：

    * **最小化到托盘**：记账工具常年开着，用户不希望任务栏里永远占一格；
    * **P6 的发薪日强制提醒**：调度器触发时若主窗口没开，
      必须能通过托盘气泡把用户拉回来——托盘是"应用还活着"的唯一凭据。

实现策略
--------
``pystray`` 与 ``pillow`` 都是可选依赖：导入失败时本模块返回 ``None``，
调用方只需判空即可，**不允许因为托盘不可用而影响主流程**。
托盘图标运行在独立线程（``run_detached``），所有菜单回调都通过
:class:`~accountbook.api.state.ShellBridge` 回到外壳，避免跨线程直接操作窗口。
"""

from __future__ import annotations

import logging
from typing import Any

from ..api.state import ShellBridge
from ..paths import AppPaths

__all__ = ["TrayHandle", "create_tray", "load_tray_image"]

_logger = logging.getLogger(__name__)


class TrayHandle:
    """托盘图标的轻量句柄，负责启动与停止。"""

    def __init__(self, icon: Any) -> None:
        self._icon = icon

    def start(self) -> None:
        """在后台线程中启动托盘事件循环。"""
        try:
            self._icon.run_detached()
            _logger.info("系统托盘已启动")
        except Exception as exc:  # noqa: BLE001 - 托盘失败不影响主程序
            _logger.warning("系统托盘启动失败（已忽略）：%s", exc)

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("停止托盘时出现异常（已忽略）：%s", exc)

    def notify(self, title: str, message: str) -> None:
        """发送气泡通知（P6 发薪日提醒会用到）。"""
        try:
            self._icon.notify(message, title)
        except Exception as exc:  # noqa: BLE001 - 部分系统不支持通知
            _logger.debug("托盘通知失败（已忽略）：%s", exc)


def load_tray_image(paths: AppPaths) -> Any | None:
    """载入托盘图标。

    优先使用打包资源中的 ``app.ico``；缺失时用 Pillow 现场绘制一个
    极简圆形图标作为兜底 —— 宁可用一个朴素图标，也不要因为缺资源而失去托盘。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001 - Pillow 不可用
        _logger.warning("Pillow 不可用，无法创建托盘图标：%s", exc)
        return None

    icon_path = paths.app_icon
    if icon_path.exists():
        try:
            with Image.open(icon_path) as img:
                # 托盘使用 64px 更清晰（高 DPI 下由系统缩放）
                return img.convert("RGBA").copy()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("读取图标资源失败，改用内置兜底图标：%s", exc)

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 系统蓝渐变近似：外圈深一些、内圈亮一些，形成一点层次
    draw.ellipse((4, 4, size - 4, size - 4), fill=(10, 132, 255, 255))
    draw.ellipse((18, 18, size - 18, size - 18), fill=(245, 245, 247, 235))
    return image


def create_tray(paths: AppPaths, shell: ShellBridge, *, app_name: str = "记账本") -> TrayHandle | None:
    """创建托盘图标；不可用时返回 ``None``。

    菜单刻意保持极简（显示 / 隐藏 / 退出）。复杂的任务与提醒入口
    属于 P6 的定时任务面板，不应挤在托盘菜单里。
    """
    try:
        import pystray
    except Exception as exc:  # noqa: BLE001 - 可选依赖缺失
        _logger.info("未安装 pystray，跳过系统托盘：%s", exc)
        return None

    image = load_tray_image(paths)
    if image is None:
        return None

    def _on_show(icon: Any, item: Any) -> None:
        # 参数由 pystray 回调签名固定，此处刻意不使用
        shell.show_window()

    def _on_quit(icon: Any, item: Any) -> None:
        # 参数由 pystray 回调签名固定，此处刻意不使用
        try:
            icon.stop()
        finally:
            shell.quit()

    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口", _on_show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _on_quit),
    )

    try:
        icon = pystray.Icon(name="AccountBook", icon=image, title=app_name, menu=menu)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("创建托盘图标失败（已忽略）：%s", exc)
        return None

    return TrayHandle(icon)
