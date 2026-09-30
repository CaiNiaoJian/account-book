"""CLR 运行时选择（pythonnet 的 .NET Framework / .NET Core 双后端问题）。

问题背景
--------
pywebview 的 Windows 后端依赖 pythonnet，而 pythonnet 需要宿主一个 .NET 运行时。
pythonnet 默认选择 **.NET Framework（netfx）**，但在部分机器上会出现：

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize

同一个 ``Python.Runtime.dll`` 在 **.NET Core（coreclr）** 宿主下工作正常，
说明这是 netfx 加载路径的兼容性问题，不是缺少组件。
本项目在开发机上实测：netfx 失败、coreclr（.NET 7）成功。

为什么不能简单地固定用 coreclr
------------------------------
* 不是每台 Windows 都装了 .NET Core 运行时（虽然 Win11 常见）；
* 反之，netfx 在大多数机器上是正常的，固定切换等于给所有人引入新风险。

因此采取「**先探测、再决定、并缓存**」的策略：

1. 在**子进程**中试探（绝不在当前进程里试）——
   一个进程只能宿主一个 CLR，若在自身进程里先失败再切 coreclr，
   第二次初始化大概率失败，而且状态已污染；
2. 依次尝试 netfx → coreclr，用**退出码**判断结果
   （windowed 打包产物没有 stdout，退出码是唯一可靠的跨形态信道）；
3. 结果写入 ``<data>/runtime-cache.json``，之后启动**零额外开销**；
4. 全部失败时返回 ``None``，上层据此降级为浏览器外壳，而不是崩溃。

安全边界：``--probe-clr`` 分支在获取单实例锁**之前**返回，
否则探测子进程会被主实例的锁挡在门外，永远得不到结论。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..paths import is_frozen

if TYPE_CHECKING:
    from ..paths import AppPaths

__all__ = ["CANDIDATE_RUNTIMES", "PROBE_FLAG", "coreclr_runtime_config", "resolve_clr_runtime"]

_logger = logging.getLogger(__name__)

#: 探测顺序：先默认（netfx，兼容面最广），再 coreclr（应对 netfx 的兼容性问题）
CANDIDATE_RUNTIMES: tuple[str, ...] = ("netfx", "coreclr")

#: 内部命令行开关。刻意不在 --help 中宣传：它是给程序自己用的
PROBE_FLAG = "--probe-clr"

#: 单次探测的超时（秒）。首次启动 .NET 运行时可能需要几秒，给足余量
_PROBE_TIMEOUT = 45.0

#: 探测进程返回此退出码表示该运行时可用（其余一律视为不可用）
_PROBE_OK_EXIT_CODE = 0

#: CoreCLR 宿主配置文件（随包分发，位于 resources/clr/）。
#:
#: 为什么必须有它：pythonnet 默认的 CoreCLR 配置只引用 ``Microsoft.NETCore.App``，
#: 而 ``System.Windows.Forms`` 属于 ``Microsoft.WindowsDesktop.App``。
#: 少了这一步，coreclr 能启动、CLR 能加载，但 pywebview 一 import WinForms 就报
#: ``Could not load file or assembly 'System.Windows.Forms'``。
RUNTIMECONFIG_FILENAME = "python.runtimeconfig.json"

#: 环境变量名（由 pythonnet 读取，不是我们发明的约定）
_CORECLR_RUNTIME_CONFIG_ENV = "PYTHONNET_CORECLR_RUNTIME_CONFIG"


def coreclr_runtime_config(paths: AppPaths) -> Path:
    """返回随包的 CoreCLR 宿主配置文件路径。"""
    return paths.resources / "clr" / RUNTIMECONFIG_FILENAME


def _apply_runtime_env(runtime: str, paths: AppPaths) -> None:
    """把候选运行时写入环境变量，供探测子进程与当前进程共用。

    只设置、不校验：真正的结论由探测的退出码给出。
    """
    os.environ["PYTHONNET_RUNTIME"] = runtime
    if runtime != "coreclr":
        return
    # 用户显式指定过就尊重用户（便于高级排障），否则用随包配置
    if os.environ.get(_CORECLR_RUNTIME_CONFIG_ENV):
        return
    config = coreclr_runtime_config(paths)
    if config.exists():
        os.environ[_CORECLR_RUNTIME_CONFIG_ENV] = str(config)
    else:  # pragma: no cover - 仅在资源被误删时发生
        _logger.warning("缺少 CoreCLR 宿主配置 %s，coreclr 可能无法加载 WinForms", config)


def _probe_command(runtime: str) -> list[str]:
    """构造探测命令。

    * 打包运行：调用自身 exe 的 ``--probe-clr`` 分支（windowed 程序无 stdout，
      因此只看退出码）；
    * 源码运行：直接让解释器 import clr，避免多绕一层。
    """
    if is_frozen():
        return [sys.executable, PROBE_FLAG, runtime]
    code = f"import os; os.environ['PYTHONNET_RUNTIME']={runtime!r}; import clr"
    return [sys.executable, "-c", code]


def _probe(runtime: str) -> bool:
    """在子进程中探测指定运行时是否可用。"""
    command = _probe_command(runtime)
    try:
        # 命令完全由本进程构造（sys.executable + 固定参数），不含任何外部输入
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            # 打包产物是 GUI 程序：不要弹出控制台窗口
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("CLR 运行时 %s 探测失败（无法执行）：%s", runtime, exc)
        return False

    ok = completed.returncode == _PROBE_OK_EXIT_CODE
    if not ok:
        # 只记录尾部若干字节，避免把整段堆栈灌进日志
        detail = (completed.stderr or b"")[-400:].decode("utf-8", errors="replace")
        _logger.debug("CLR 运行时 %s 不可用（退出码 %s）：%s", runtime, completed.returncode, detail)
    return ok


def _load_cache(path: Path) -> str | None:
    """读取缓存；任何异常都视为"没有缓存"。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    runtime = payload.get("clr_runtime")
    return runtime if isinstance(runtime, str) and runtime in CANDIDATE_RUNTIMES else None


def _save_cache(path: Path, runtime: str | None) -> None:
    """写入缓存；失败不影响本次运行。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"clr_runtime": runtime, "probed_by": "shell.clr_runtime"}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        _logger.debug("CLR 运行时缓存写入失败（不影响本次启动）：%s", exc)


def resolve_clr_runtime(paths: AppPaths) -> str | None:
    """确定并设置 ``PYTHONNET_RUNTIME`` 环境变量。

    返回最终选定的运行时名；返回 ``None`` 表示没有可用运行时
    （调用方应降级为浏览器外壳）。

    调用时机：**必须在 ``import clr`` / 导入 pywebview 的 Windows 后端之前**。
    """
    explicit = os.environ.get("PYTHONNET_RUNTIME", "").strip()
    if explicit:
        _logger.info("使用环境变量指定的 CLR 运行时：%s", explicit)
        # 仍需补上 CoreCLR 宿主配置，否则用户手设 coreclr 时会缺 WinForms
        _apply_runtime_env(explicit, paths)
        return explicit

    cache_file = paths.runtime_cache_file
    cached = _load_cache(cache_file)
    if cached:
        _logger.debug("使用缓存的 CLR 运行时：%s", cached)
        _apply_runtime_env(cached, paths)
        return cached

    _logger.info("首次启动：探测可用的 CLR 运行时（候选：%s）", ", ".join(CANDIDATE_RUNTIMES))
    for runtime in CANDIDATE_RUNTIMES:
        # 环境变量必须**在探测之前**设置好：探测子进程通过继承环境变量获得同样的配置，
        # 只有这样才能保证"探测通过的组合"与"真正启动时使用的组合"完全一致。
        _apply_runtime_env(runtime, paths)
        if _probe(runtime):
            _logger.info("已选定 CLR 运行时：%s", runtime)
            _save_cache(cache_file, runtime)
            return runtime

    _logger.warning(
        "未找到可用的 .NET 运行时，原生窗口不可用。"
        "请安装 .NET Framework 4.7.2+ 或 .NET 6+ 运行时；"
        "在此之前应用将以浏览器外壳运行（功能完全一致）。"
    )
    # 缓存"无可用运行时"，避免每次启动都重复探测（用户装好运行时后删除该文件即可重试）
    _save_cache(cache_file, None)
    return None
