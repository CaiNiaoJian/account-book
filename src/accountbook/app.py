"""应用编排 —— 把「日志 / 单实例 / 后端服务 / 桌面外壳 / 生命周期」串起来。

启动顺序（每一步失败都有明确的降级策略）
----------------------------------------
1. 解析运行设置与路径          —— 失败：直接退出（无法确定数据位置就不该继续）
2. 初始化日志与崩溃兜底        —— 失败：降级为仅控制台输出
3. 获取单实例锁                —— 已有实例：提示并退出（避免 SQLite 写冲突）
4. 读取用户偏好                —— 损坏：回退默认值并记录，不阻塞启动
5. 启动本地 HTTP 服务          —— 端口由系统分配，随后探活确认
6. 创建桌面外壳并加载页面      —— pywebview 不可用：降级为浏览器外壳
7. 进入 GUI 事件循环           —— 阻塞至窗口关闭
8. 收敛：停后端 → 停托盘 → 释放锁

为什么要显式地"探活"再开窗：
    WebView 加载一个还没监听的端口会显示错误页，而用户只会看到"应用坏了"。
    用几毫秒换一次可靠的等待，是值得的（这里最多等 5 秒）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import APP_NAME_EN, BUILD_PHASE, __version__
from .api.server import create_app
from .api.state import AppContext, create_context
from .config import RuntimeSettings, build_config_store
from .core.single_instance import InstanceLock
from .logging_setup import install_excepthook, setup_logging
from .paths import AppPaths, get_paths
from .shell.tray import TrayHandle, create_tray
from .shell.window import BrowserShell, ShellAdapter, create_shell

__all__ = ["BackendServer", "run"]

_logger = logging.getLogger(__name__)

#: 等待后端就绪的最长时间（秒）。超时说明环境异常，但要给出可操作的提示。
_READY_TIMEOUT = 5.0

#: 探活轮询间隔（秒）。10ms 级别在常规机器上只需几次即可成功。
_READY_POLL_INTERVAL = 0.02


@dataclass
class _Shutdown:
    """退出时的资源清单，保证"无论从哪条路径退出"都能收敛。"""

    lock: InstanceLock | None = None
    server: BackendServer | None = None
    tray: TrayHandle | None = None


class BackendServer:
    """在后台线程中运行 uvicorn，并暴露实际监听的端口。

    关键实现选择：**自己先 bind socket，再交给 uvicorn**。
    这样端口在 uvicorn 启动之前就是确定的（``port=0`` 由系统分配），
    避免"启动后再去问 uvicorn 用了哪个端口"这种竞态。
    """

    def __init__(self, ctx: AppContext, settings: RuntimeSettings) -> None:
        self.ctx = ctx
        self.settings = settings
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    # ---- 启动 / 停止 --------------------------------------------------------
    def start(self) -> int:
        """启动服务并返回实际监听端口。"""
        import uvicorn

        app = create_app(self.ctx)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            requested = self.settings.dev_backend_port if self.settings.dev else 0
            sock.bind(("127.0.0.1", requested))
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"无法绑定本机端口：{exc}") from exc
        sock.listen(128)
        self._socket = sock
        port = int(sock.getsockname()[1])

        # 只监听环回地址：这是"不上云"在网络层面的硬性保证，不是可配置项。
        config = uvicorn.Config(
            app=app,
            log_config=None,  # 日志交给本项目自己的 logging 配置
            log_level="warning",
            access_log=False,  # 本地应用的访问日志是纯噪声
            lifespan="on",
            timeout_graceful_shutdown=int(self.settings.shutdown_grace_seconds),
        )
        server = uvicorn.Server(config)
        self._server = server

        self._thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            name="accountbook-http",
            daemon=True,  # 守护线程：主线程退出时不会挂住进程
        )
        self._thread.start()

        self._wait_until_ready(port)
        _logger.info("本地服务已就绪：http://127.0.0.1:%s", port)
        return port

    def stop(self) -> None:
        """请求 uvicorn 优雅停机，并等待线程收敛。"""
        server = self._server
        if server is None:
            return
        try:
            server.should_exit = True  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 尽力而为
            _logger.debug("设置停机标记失败：%s", exc)

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.settings.shutdown_grace_seconds)
            if thread.is_alive():
                _logger.warning(
                    "后端线程未在 %.1fs 内退出，将由进程退出回收", self.settings.shutdown_grace_seconds
                )

        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None

    # ---- 内部 ---------------------------------------------------------------
    def _wait_until_ready(self, port: int) -> None:
        """轮询 ``/health``，确认服务真正可用。

        只做 TCP 连接是不够的：uvicorn 可能已绑定端口但应用尚未完成启动，
        此时开窗会命中 502/连接重置。因此这里真的发一次 HTTP 请求。
        """
        deadline = time.monotonic() + _READY_TIMEOUT
        url = f"http://127.0.0.1:{port}/health"
        last_error: str = "未知原因"

        while time.monotonic() < deadline:
            try:
                # 访问的是本进程刚绑定的环回地址，不接受外部输入
                with urllib.request.urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return
                    last_error = f"HTTP {response.status}"
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(_READY_POLL_INTERVAL)

        raise RuntimeError(
            f"后端服务在 {_READY_TIMEOUT:.0f} 秒内未就绪（{last_error}）。请查看日志了解详情。"
        )


def run() -> int:
    """应用主入口。返回进程退出码（0 表示正常结束）。"""
    settings = RuntimeSettings()

    # ---- 1. 路径（必须先于日志，因为日志要写进数据目录） --------------------
    paths: AppPaths = get_paths()
    try:
        paths.ensure()
    except OSError as exc:
        # 数据目录不可写是**启动前**的致命问题（例如安装在 Program Files 且无权限、
        # 磁盘已满、被安全软件拦截）。此时日志系统尚未初始化，因此直接给出
        # 人类可读的提示，并附上可操作的解决办法，而不是抛一段堆栈。
        _report_data_dir_unusable(paths, exc)
        return 2

    # ---- 2. 日志与崩溃兜底 --------------------------------------------------
    setup_logging(paths.log_file, settings)
    install_excepthook(paths.logs)
    _log_banner(paths, settings)

    shutdown = _Shutdown()

    # ---- 3. 单实例锁 --------------------------------------------------------
    if settings.single_instance:
        lock = InstanceLock(paths.lock_file)
        result = lock.acquire()
        if not result.acquired:
            _logger.warning("检测到已有实例：%s", result.holder)
            _report_already_running(paths, result.holder)
            return 0
        shutdown.lock = lock

    try:
        # ---- 4. 偏好 --------------------------------------------------------
        config = build_config_store(paths)
        prefs = config.snapshot()
        _logger.info(
            "用户偏好：theme=%s language=%s reduce_motion=%s privacy=%s",
            prefs.theme,
            prefs.language,
            prefs.reduce_motion,
            prefs.privacy_mode,
        )

        # ---- 5. 后端 --------------------------------------------------------
        ctx = create_context(paths=paths, settings=settings, config=config)
        server = BackendServer(ctx, settings)
        shutdown.server = server
        ctx.port = server.start()

        url = _build_entry_url(ctx, settings)
        _logger.info("界面入口：%s", _mask_token(url))

        # ---- 6a. 仅服务模式：不建窗口，只保持本地服务运行 -------------------
        # 用途：端到端测试与截图验证、无桌面会话的环境、
        # 以及偏好用自己浏览器的用户。此时进程靠 Ctrl+C 或停机信号结束。
        if settings.serve_only:
            # flush=True：输出被重定向到管道/文件时 stdout 是块缓冲的，
            # 不刷新会让调用方（自动化脚本、CI）迟迟拿不到入口地址。
            _emit(f"[AccountBook] 本地服务已启动：{url}")
            _emit("[AccountBook] 按 Ctrl+C 结束进程。")
            try:
                while not ctx.shutdown_event.wait(0.5):
                    pass
            except KeyboardInterrupt:
                _logger.info("收到中断信号，准备退出")
            return 0

        # ---- 6b. 桌面外壳 ---------------------------------------------------
        shell: ShellAdapter = create_shell(ctx, settings, url)

        # 主题变更 → 同步原生标题栏。订阅在这里注册，保证"任何来源的变更"
        # （前端 PATCH、未来托盘菜单、配置文件热改）都能生效。
        config.subscribe(lambda updated: shell.apply_theme(updated.theme))

        tray: TrayHandle | None = None
        if settings.enable_tray:
            tray = create_tray(paths, shell, app_name=f"{APP_NAME_EN} {__version__}")
            if tray is not None:
                shutdown.tray = tray
                tray.start()

        # 首次启动标记：用于 P11 的"欢迎向导"，P0 只做记录
        if not prefs.first_run_completed:
            config.update(first_run_completed=True, last_version=__version__)
        elif prefs.last_version != __version__:
            config.update(last_version=__version__)

        # ---- 7. 进入事件循环（阻塞） ----------------------------------------
        # 关键：外壳启动失败**不能**让整个应用挂掉。
        # pywebview 依赖 CLR，即便运行时探测通过，仍可能在创建窗口时因
        # WebView2 缺失、策略限制等失败。此时降级为浏览器外壳 ——
        # 同一套前端、同一套 API、同样的令牌，功能完全等价。
        _logger.info("进入桌面外壳事件循环（kind=%s）", shell.kind)
        try:
            shell.start()
        except Exception:
            _logger.exception("桌面外壳启动失败，尝试降级为浏览器外壳")
            if isinstance(shell, BrowserShell):
                raise  # 已经是降级形态还失败，那就是真问题
            fallback = BrowserShell(ctx, settings, url)
            ctx.bind_shell(fallback)
            _logger.warning("已降级为浏览器外壳；界面地址：%s", _mask_token(url))
            fallback.start()

    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        _logger.info("收到中断信号，正在退出")
    except Exception:
        _logger.exception("应用启动失败")
        return 1
    finally:
        # ---- 8. 收敛 --------------------------------------------------------
        _logger.info("开始退出流程")
        if shutdown.tray is not None:
            shutdown.tray.stop()
        if shutdown.server is not None:
            shutdown.server.stop()
        if shutdown.lock is not None:
            shutdown.lock.release()
        _logger.info("已退出")

    return 0


# -----------------------------------------------------------------------------
# 辅助
# -----------------------------------------------------------------------------
def _build_entry_url(ctx: AppContext, settings: RuntimeSettings) -> str:
    """构造窗口加载的入口 URL。

    * 生产：由本进程的 HTTP 服务托管前端；
    * 开发：加载 Vite 开发服务器（由它把 ``/api`` 代理回本后端），
      这样前端拥有热更新能力，而 API 仍是同一套代码。

    令牌以查询参数传入：这是页面**首次**加载时唯一的凭据来源，
    服务端在校验通过后会种下 HttpOnly Cookie，之后前端无需再处理令牌。
    """
    base = settings.dev_server_url.rstrip("/") if settings.dev else f"http://127.0.0.1:{ctx.port}"
    return f"{base}/?token={ctx.token.value}"


def _mask_token(url: str) -> str:
    """日志中脱敏令牌：保留前缀便于排查，主体打码。"""
    if "token=" not in url:
        return url
    head, _, token = url.partition("token=")
    return f"{head}token={token[:6]}…({len(token)} chars)"


def _log_banner(paths: AppPaths, settings: RuntimeSettings) -> None:
    """启动横幅：一次性记录排查所需的全部环境事实（不含用户数据）。"""
    _logger.info("=" * 74)
    _logger.info("%s %s（阶段 %s）", APP_NAME_EN, __version__, BUILD_PHASE)
    _logger.info("进程 PID=%s  Python=%s", os.getpid(), sys.version.split()[0])
    for key, value in paths.describe().items():
        _logger.info("  %-14s %s", key, value)
    _logger.info(
        "  dev=%s single_instance=%s tray=%s", settings.dev, settings.single_instance, settings.enable_tray
    )
    _logger.info("=" * 74)


def _emit(text: str) -> None:
    """安全地向标准输出打印一行。

    为什么需要它：PyInstaller 的 **windowed**（``console=False``）构建里
    ``sys.stdout`` 可能是 ``None``，此时 ``print()`` 会抛 AttributeError ——
    一个"没有控制台所以打印失败导致程序崩溃"的荒谬故障。
    这里统一兜住，保证任何运行形态下输出都不会成为故障源。
    """
    if sys.stdout is None:
        return
    with contextlib.suppress(OSError, ValueError, AttributeError):
        # 管道已关闭 / 无控制台等情况：放弃输出，不影响主流程
        print(text, flush=True)


def _report_data_dir_unusable(paths: AppPaths, exc: OSError) -> None:
    """数据目录不可用时的用户可读提示（启动前致命错误）。

    常见原因与就地给出的解决办法：
        * 程序被放在 ``C:\\Program Files`` 且当前用户无写权限
          → 改用安装版（数据会落在 %LOCALAPPDATA%）或以管理员身份运行一次；
        * 便携版被放在只读介质（光盘、只读 U 盘）
          → 复制到可写目录，或用 ``--data-dir`` 指定位置；
        * 磁盘已满 / 被安全软件拦截
          → 清理空间或调整安全软件规则。

    这里刻意**不打印堆栈**：用户看不懂，而支持人员可以从 ``--print-paths`` 拿到同样信息。
    """
    message = (
        f"无法创建或写入数据目录，程序已停止启动。\n\n"
        f"数据目录：{paths.data}\n"
        f"运行模式：{'便携版' if paths.portable else '安装版'}\n"
        f"错误信息：{exc}\n\n"
        "可以尝试：\n"
        "  1) 把程序复制到你有写权限的目录（例如桌面或文档）；\n"
        '  2) 用命令行指定数据位置：accountbook --data-dir "D:\\我的账本"\n'
        "  3) 检查磁盘剩余空间与安全软件拦截记录。"
    )
    _emit(message)
    _show_message_box(message, title=APP_NAME_EN)


def _report_already_running(paths: AppPaths, holder: str | None) -> None:
    """已有实例时的用户可读提示。

    以 Windows 消息框呈现（若可用），否则退回控制台输出。
    刻意**不**自动唤起已有窗口：跨进程唤起需要进程间通信，
    P0 不做过度设计，先保证"不会静默什么都没发生"。
    """
    message = (
        f"{APP_NAME_EN} 已在运行。\n\n"
        "为避免账本数据被两个进程同时写入，本次启动已取消。\n"
        "请切换到已打开的窗口；若找不到窗口，请在任务管理器中结束旧进程后重试。\n\n"
        f"数据目录：{paths.data}\n"
        f"占用信息：{holder or '未知'}"
    )
    _logger.info(message)
    _show_message_box(message, title=APP_NAME_EN)


def _show_message_box(message: str, *, title: str) -> None:
    """显示一个信息型消息框（仅 Windows；失败时静默）。

    统一封装的原因：消息框在两个失败分支中都要用，且都必须"宁可没提示，
    也不能因为提示本身再抛异常"。
    """
    try:
        if sys.platform == "win32":
            import ctypes

            # MB_ICONINFORMATION(0x40) | MB_SETFOREGROUND(0x10000) | MB_TOPMOST(0x40000)
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40 | 0x10000 | 0x40000)
    except Exception as exc:  # noqa: BLE001 - 提示失败不影响退出码
        _logger.debug("显示消息框失败：%s", exc)
