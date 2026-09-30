"""进程入口 —— ``python -m accountbook`` 或安装后的 ``accountbook`` 命令。

设计约定
--------
* **命令行参数只覆盖环境变量，不发明第二套配置体系**：
  ``--dev`` 等价于 ``ACCOUNTBOOK_DEV=1``，实现上直接写 ``os.environ``，
  这样后续新增参数无需在配置层再开一条通路。
* **诊断类参数（--print-paths / --doctor）不启动界面**：
  用户与支持人员需要在"应用打不开"时仍能拿到环境信息。
  这是"谨慎"原则的体现——把可观测性做进入口，而不是等到出事再加。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import APP_NAME_EN, BUILD_PHASE, __version__


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="accountbook",
        description=f"{APP_NAME_EN} —— 本地优先、离线可用的个人记账本桌面工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  accountbook                    正常启动\n"
            "  accountbook --dev              配合 npm run dev 做前端热更新开发\n"
            "  accountbook --data-dir D:\\账本  使用指定数据目录（多档案隔离）\n"
            "  accountbook --print-paths      打印路径与环境信息后退出（排查用）\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME_EN} {__version__} ({BUILD_PHASE})")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="开发模式：加载 Vite 开发服务器，后端使用固定端口 8787",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="覆盖数据目录（等价于环境变量 ACCOUNTBOOK_DATA_DIR）",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="强制使用系统浏览器作为外壳（跳过 pywebview 原生窗口）",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="覆盖日志级别",
    )
    parser.add_argument(
        "--webview-debug",
        action="store_true",
        help="打开 WebView2 开发者工具（配合 --dev 调试前端）",
    )
    parser.add_argument(
        "--serve-only",
        action="store_true",
        help="只启动本地服务，不创建桌面窗口（用于端到端测试或使用自带浏览器）",
    )
    # 内部开关：供程序自身探测 .NET 运行时（见 shell/clr_runtime.py）。
    # 不做隐藏，但明确标注为内部用途，避免被当成用户功能。
    parser.add_argument(
        "--probe-clr",
        metavar="RUNTIME",
        choices=["netfx", "coreclr"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--print-paths",
        action="store_true",
        help="打印路径、运行模式与版本信息（JSON）后退出，不启动界面",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="环境自检：检查 WebView2 / pywebview / 依赖可用性后退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口函数（带引导期崩溃报告）。返回进程退出码。

    为什么要在最外层再包一层 try/except
    -----------------------------------
    打包为 windowed（无控制台）程序后，**任何**在日志系统初始化之前发生的异常
    都会让进程静默消失：用户双击图标，什么也没发生，也没有任何线索。
    这是 Windows 上最糟糕的失败模式。

    因此这里做两件事（顺序很重要）：
        1. 先把完整 traceback 写到 ``%TEMP%/AccountBook-startup-error.txt``；
        2. 再弹一个说明对话框。
    先落盘再弹窗，是因为在自动化环境里对话框可能无人点击而一直阻塞 ——
    哪怕界面卡住，排查者依然能从文件里拿到真相。
    """
    try:
        return _main(argv)
    except SystemExit:
        raise  # argparse 的正常退出路径（--help / --version）
    except BaseException:  # noqa: BLE001 - 引导期必须兜住一切
        return _report_bootstrap_failure()


def _main(argv: list[str] | None) -> int:
    """真正的入口实现（不处理引导期异常）。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    # ---- 参数 → 环境变量（统一在配置层读取） --------------------------------
    import os

    if args.dev:
        os.environ["ACCOUNTBOOK_DEV"] = "1"
    if args.data_dir:
        os.environ["ACCOUNTBOOK_DATA_DIR"] = args.data_dir
    if args.browser:
        os.environ["ACCOUNTBOOK_SHELL"] = "browser"
    if args.log_level:
        os.environ["ACCOUNTBOOK_LOG_LEVEL"] = args.log_level
    if args.webview_debug:
        os.environ["ACCOUNTBOOK_WEBVIEW_DEBUG"] = "1"
        # 调试时几乎必然需要更详细的日志
        os.environ.setdefault("ACCOUNTBOOK_LOG_LEVEL", "DEBUG")
    if args.serve_only:
        os.environ["ACCOUNTBOOK_SERVE_ONLY"] = "1"
        # 无窗口时托盘没有意义，且会干扰自动化脚本的退出
        os.environ.setdefault("ACCOUNTBOOK_ENABLE_TRAY", "0")

    # ---- 内部：CLR 运行时探测（必须在任何重型导入之前） ---------------------
    # 返回码是唯一可靠的跨形态信道：windowed 打包产物没有 stdout。
    if args.probe_clr:
        os.environ["PYTHONNET_RUNTIME"] = args.probe_clr
        try:
            import clr  # noqa: F401

            return 0
        except Exception:  # noqa: BLE001 - 探测失败就是结论本身
            return 4

    # ---- 不启动界面的诊断分支 ----------------------------------------------
    if args.print_paths:
        return _print_paths()
    if args.doctor:
        return _doctor()

    # 延迟导入：--version / --print-paths 等分支不应触发重型依赖加载
    from .app import run

    return run()


def _report_bootstrap_failure() -> int:
    """引导期异常的统一出口：落盘 + 弹窗 + 返回可区分的退出码。"""
    import os
    import sys
    import tempfile
    import traceback

    trace = traceback.format_exc()
    report_path = os.path.join(tempfile.gettempdir(), "AccountBook-startup-error.txt")
    header = (
        f"{APP_NAME_EN} {__version__} ({BUILD_PHASE}) 启动失败\n"
        f"Python: {sys.version.split()[0]}\n"
        f"可执行文件: {sys.executable}\n"
        f"命令行: {' '.join(sys.argv)}\n"
        f"{'-' * 74}\n"
    )
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(header + trace)
    except OSError:
        report_path = "(无法写入报告文件)"

    message = (
        f"{APP_NAME_EN} 启动失败，已中止。\n\n"
        f"错误报告已保存到：\n{report_path}\n\n"
        "请把该文件连同问题描述一起反馈；其中包含完整的错误堆栈，"
        "但不包含任何账目数据。"
    )
    try:
        if sys.stdout is not None:
            print(message, flush=True)
    except (OSError, ValueError):
        pass

    # 只在"无控制台"的情况下弹窗（即打包后的 GUI 程序，用户双击启动）：
    #   * 有控制台时消息已经打印出来，再弹一个需要点击的模态框只会阻塞自动化脚本；
    #   * 无控制台时若不弹窗，用户就真的什么都看不到。
    if sys.stdout is not None:
        return 3

    try:
        import ctypes

        # MB_ICONERROR(0x10) | MB_SETFOREGROUND(0x10000) | MB_TOPMOST(0x40000)
        ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME_EN} 启动失败", 0x10 | 0x10000 | 0x40000)
    except Exception:  # noqa: BLE001 - 弹窗失败不影响退出码
        pass

    return 3


def _print_paths() -> int:
    """打印路径与环境信息（JSON）后退出。"""
    from .config import RuntimeSettings
    from .paths import get_paths

    paths = get_paths()
    settings = RuntimeSettings()
    payload = {
        "version": __version__,
        "phase": BUILD_PHASE,
        **paths.describe(),
        "settings": {
            "dev": settings.dev,
            "single_instance": settings.single_instance,
            "enable_tray": settings.enable_tray,
            "gui_backend": settings.gui_backend,
            "log_level": settings.log_level,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _doctor() -> int:
    """环境自检：逐项检查并给出**可操作**的结论。

    输出分三类：``ok`` / ``warn`` / ``fail``。
    退出码：全部 ok 或仅 warn → 0；存在 fail → 2。
    """
    import importlib
    import platform

    from .paths import get_paths

    checks: list[tuple[str, str, str]] = []

    def record(level: str, name: str, detail: str) -> None:
        checks.append((level, name, detail))

    # Python 版本
    py = sys.version_info
    if py >= (3, 11):
        record("ok", "python", f"{platform.python_version()}")
    else:
        record("fail", "python", f"{platform.python_version()}（需要 3.11+）")

    # 平台
    record(
        "ok" if sys.platform == "win32" else "warn", "platform", f"{platform.system()} {platform.release()}"
    )

    # 必需依赖
    for module, purpose, required in (
        ("fastapi", "HTTP 接口层", True),
        ("uvicorn", "ASGI 服务器", True),
        ("pydantic", "数据校验", True),
        ("webview", "原生窗口外壳（缺失时降级浏览器）", False),
        ("clr", "pythonnet（WebView2 后端依赖）", False),
        ("pystray", "系统托盘（可选）", False),
        ("PIL", "图标与图像处理（托盘/导出需要）", False),
    ):
        try:
            importlib.import_module(module)
            record("ok", f"import {module}", purpose)
        except Exception as exc:  # noqa: BLE001
            record("fail" if required else "warn", f"import {module}", f"{purpose} —— {exc}")

    # pythonnet 的 CLR 初始化（只有真正加载才能确认，import clr 会触发）
    try:
        import clr  # noqa: F401

        record("ok", "CLR 初始化", "pythonnet 可用")
    except Exception as exc:  # noqa: BLE001
        record("warn", "CLR 初始化", f"不可用（将降级为浏览器外壳）：{exc}")

    # WebView2 运行时（注册表探测，无需启动浏览器）
    if sys.platform == "win32":
        try:
            import winreg

            key_path = (
                r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
                r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
            )
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
            record("ok", "WebView2 运行时", str(version))
        except Exception as exc:  # noqa: BLE001
            record(
                "warn",
                "WebView2 运行时",
                f"未检测到（{exc}）；原生窗口将不可用，请安装 Microsoft Edge WebView2 Runtime",
            )

    # 路径可写性
    try:
        paths = get_paths()
        paths.ensure()
        probe = paths.cache / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("ok", "数据目录可写", str(paths.data))
        if paths.degraded:
            record("warn", "便携模式", "目标目录不可写，已降级到用户数据目录")
    except Exception as exc:  # noqa: BLE001
        record("fail", "数据目录可写", f"{exc}")

    # ---- 输出 ---------------------------------------------------------------
    symbols = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    width = max(len(name) for _, name, _ in checks) if checks else 10
    print(f"{APP_NAME_EN} {__version__} ({BUILD_PHASE}) 环境自检\n")
    for level, name, detail in checks:
        print(f"{symbols[level]} {name.ljust(width)}  {detail}")

    failures = sum(1 for level, _, _ in checks if level == "fail")
    warnings = sum(1 for level, _, _ in checks if level == "warn")
    print(f"\n结果：{len(checks)} 项检查，{failures} 项失败，{warnings} 项警告")
    if failures:
        print("请先解决标记为 [FAIL] 的问题。")
    elif warnings:
        print("核心功能可用；带 [WARN] 的项只影响可选能力。")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
