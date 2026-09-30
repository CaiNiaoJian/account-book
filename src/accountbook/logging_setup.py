"""日志与崩溃兜底。

目标（按重要性排序）：
    1. **出问题时有据可查**：轮转文件日志 + 未捕获异常完整堆栈 + 崩溃转储。
    2. **不泄漏隐私**（REQ-14）：日志默认不打印金额与商户名，
       因此本模块提供 :func:`redact_money` 与 :func:`redact_text`，
       供上层在确实需要记录金额时显式脱敏后写入。
    3. **不打扰用户**：打包运行时不弹控制台窗口，日志只落文件。

线程安全：logging 模块自身线程安全；文件切换由 ``RotatingFileHandler`` 负责。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from .config import RuntimeSettings

__all__ = ["install_excepthook", "redact_money", "redact_text", "setup_logging"]

_logger = logging.getLogger(__name__)

#: 日志格式：时间(本地) | 级别 | 模块:行号 | 消息
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 金额脱敏：把「¥1,234.56」「1234.56 元」「$12.30」统一替换为 ¥*** / $***
_MONEY_PATTERNS = (
    re.compile(r"[¥￥]\s?\d[\d,]*(\.\d+)?"),
    re.compile(r"[$€£]\s?\d[\d,]*(\.\d+)?"),
    re.compile(r"\d[\d,]*(\.\d+)?\s?(元|块|美元|欧元)"),
)

# 商户/人名等自由文本：仅保留首尾各 1 个字符
_MAX_INLINE_TEXT = 24


def redact_money(text: str) -> str:
    """把文本中的金额替换为 ``***``，用于可能被写入日志的用户可见字符串。"""
    result = text
    for pattern in _MONEY_PATTERNS:
        result = pattern.sub("***", result)
    return result


def redact_text(text: str, keep: int = 1) -> str:
    """保留首尾少量字符的文本脱敏，用于商户名、备注等（REQ-14）。

    过短的文本直接整体打码，避免"脱敏后仍可识别"。
    """
    if len(text) <= keep * 2 + 1:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * (len(text) - keep * 2)}{text[-keep:]}"


def setup_logging(paths_log_file: Path, settings: RuntimeSettings) -> logging.Logger:
    """初始化根日志器：轮转文件 + 可选控制台。

    参数
    ----
    paths_log_file:
        日志文件路径（通常为 ``<data>/logs/accountbook.log``）。
    settings:
        运行时设置，读取 ``log_level`` / ``log_max_bytes`` / ``log_backup_count`` /
        ``log_to_console``。

    返回根日志器，便于调用方直接使用。
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # 幂等：重复初始化（例如测试中多次调用）不叠加 handler
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # ---- 文件 handler（UTF-8：中文日志不能变乱码） --------------------------
    try:
        paths_log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            paths_log_file,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
            delay=True,  # 延迟到第一条日志才创建文件，避免"启动即产生空文件"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except OSError as exc:  # 磁盘满 / 目录只读：不能让日志系统拖垮程序
        print(f"[AccountBook] 日志文件初始化失败（将仅使用控制台）：{exc}", file=sys.stderr)

    # ---- 控制台 handler -----------------------------------------------------
    # 打包运行时 stdout 可能不存在（无控制台窗口），此时跳过。
    if settings.log_to_console and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    # 第三方库降噪：uvicorn 的访问日志对本地应用毫无价值，且会淹没业务日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("pythonnet").setLevel(logging.WARNING)

    _logger.debug("日志系统已初始化：level=%s file=%s", settings.log_level, paths_log_file)
    return root


def install_excepthook(logs_dir: Path) -> None:
    """安装全局未捕获异常处理器：写日志 + 落崩溃文件 + 保留原始行为。

    这是"谨慎"原则的最后一道防线：任何未预料的异常都应留下证据，
    而不是让窗口静默消失（用户只会看到"打不开"）。
    """

    def _handler(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            # Ctrl+C 属于正常退出路径，交回默认处理
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        logging.getLogger("accountbook.crash").critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            crash_file = logs_dir / f"crash-{stamp}.txt"
            import traceback

            crash_file.write_text(
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                encoding="utf-8",
            )
        except OSError:
            pass  # 崩溃处理本身失败时不再递归处理

    sys.excepthook = _handler

    # 工作线程中的异常默认只打印到 stderr，同样需要落盘（uvicorn 就在线程里）
    def _thread_handler(args: Any) -> None:  # pragma: no cover - 依赖运行时触发
        _handler(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_handler
