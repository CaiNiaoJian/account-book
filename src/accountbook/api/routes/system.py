"""系统与偏好路由。

职责边界：
    * 提供**只读环境信息**（版本、路径、运行模式），供「关于」页与诊断使用；
    * 提供**用户偏好的读写**，这是前端主题/语言持久化的唯一入口。

刻意不做的事：
    * 不返回任何用户账目数据（P1 起由专门的路由负责，且需经过脱敏层）；
    * 不返回会话令牌（令牌只通过页面注入的 ``window.__AB_BOOT__`` 传递给前端，
      避免它出现在容易被复制粘贴的接口响应里）。
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ... import APP_ID, APP_NAME, APP_NAME_EN, BUILD_PHASE, __version__
from ...config import UserPreferences
from ..state import context_of

__all__ = ["router"]

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


# -----------------------------------------------------------------------------
# 响应模型
# -----------------------------------------------------------------------------
class PathsInfo(BaseModel):
    """路径信息（只含路径字符串，不含任何文件内容）。"""

    data_dir: str
    #: 当前数据目录的来源说明（例如"用户本地数据目录（%LOCALAPPDATA%）"）
    data_dir_source: str
    #: 被跳过或失败的候选位置及原因；为空表示首选位置即可用
    data_dir_attempts: list[str]
    program_root: str
    log_file: str
    database: str
    portable: bool
    frozen: bool
    degraded: bool


class RuntimeInfo(BaseModel):
    """运行环境信息 —— 排查"为什么在我机器上不一样"的第一手资料。"""

    app_id: str
    app_name: str
    app_name_en: str
    version: str
    phase: str
    python_version: str
    platform: str
    pid: int
    port: int
    uptime_seconds: float
    is_admin: bool
    #: 实际生效的外壳：pywebview（原生窗口）或 browser（降级）
    shell: str
    #: 选定的 .NET 运行时；None 表示原生外壳不可用
    clr_runtime: str | None
    paths: PathsInfo


class PreferencesPatch(BaseModel):
    """偏好局部更新载荷。

    所有字段可选 —— 前端只发它真正改动的那一项。
    ``Literal`` 类型让非法值在进入业务层之前就被 FastAPI 拒绝（422）。
    """

    theme: Literal["light", "dark", "system"] | None = None
    language: Literal["zh-CN", "en-US"] | None = None
    reduce_motion: bool | None = None
    money_color_scheme: Literal["cn", "intl"] | None = None
    privacy_mode: bool | None = None
    sidebar_collapsed: bool | None = None
    sidebar_order: list[str] | None = Field(default=None, max_length=64)
    backup_enabled: bool | None = None
    backup_interval_hours: int | None = Field(default=None, ge=1, le=8760)


class PreferencesResponse(BaseModel):
    preferences: UserPreferences


# -----------------------------------------------------------------------------
# 路由
# -----------------------------------------------------------------------------
@router.get("/info", response_model=RuntimeInfo, summary="运行环境信息")
def get_info(request: Request) -> RuntimeInfo:
    """返回版本、平台、路径与运行模式。

    注意：``data_dir`` 是**用户自己的磁盘路径**，属于本机信息，不外传；
    接口本身受令牌与环回限制保护。
    """
    ctx = context_of(request.app)
    return RuntimeInfo(
        app_id=APP_ID,
        app_name=APP_NAME,
        app_name_en=APP_NAME_EN,
        version=__version__,
        phase=BUILD_PHASE,
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        pid=os.getpid(),
        port=ctx.port,
        uptime_seconds=round(ctx.uptime_seconds(), 3),
        is_admin=_is_admin(),
        shell=ctx.shell_kind,
        clr_runtime=ctx.clr_runtime,
        # 显式逐字段构造：不用 dict 过滤。
        # 过滤写法的隐患是"字段名写错时静默丢字段"，而这里一旦漏字段
        # Pydantic 会立刻报错 —— 让错误在开发期暴露，而不是等用户发现界面上少了一行。
        paths=PathsInfo(
            data_dir=str(ctx.paths.data),
            data_dir_source=ctx.paths.data_dir_source,
            data_dir_attempts=list(ctx.paths.data_dir_attempts),
            program_root=str(ctx.paths.program_root),
            log_file=str(ctx.paths.log_file),
            database=str(ctx.paths.database),
            portable=ctx.paths.portable,
            frozen=ctx.paths.frozen,
            degraded=ctx.paths.degraded,
        ),
    )


@router.get("/preferences", response_model=PreferencesResponse, summary="读取用户偏好")
def get_preferences(request: Request) -> PreferencesResponse:
    ctx = context_of(request.app)
    return PreferencesResponse(preferences=ctx.config.snapshot())


@router.patch("/preferences", response_model=PreferencesResponse, summary="更新用户偏好")
def patch_preferences(payload: PreferencesPatch, request: Request) -> PreferencesResponse:
    """局部更新偏好。

    变更会经由 :class:`~accountbook.config.ConfigStore` 通知订阅者，
    桌面外壳据此同步 Windows 标题栏深色状态 —— 这正是"日/夜全局兼容"的落点。
    """
    ctx = context_of(request.app)
    changes: dict[str, Any] = payload.model_dump(exclude_none=True)
    if not changes:
        return PreferencesResponse(preferences=ctx.config.snapshot())
    _logger.debug("偏好更新请求：%s", sorted(changes.keys()))
    updated = ctx.config.update(**changes)
    return PreferencesResponse(preferences=updated)


@router.post("/reveal-data-dir", summary="在资源管理器中打开数据目录")
def reveal_data_dir(request: Request) -> dict[str, str]:
    """打开数据目录。

    这是**唯一**会启动外部程序的接口，因此：
        * 不接收任何路径参数（杜绝"任意命令执行"面）；
        * 目标路径固定取自服务端解析结果，客户端无法影响。
    """
    ctx = context_of(request.app)
    target = ctx.paths.data
    try:
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # 目标路径来自服务端固定解析结果，不含任何客户端输入
            os.startfile(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        _logger.warning("打开数据目录失败：%s", exc)
        return {"status": "error", "detail": str(exc), "path": str(target)}
    return {"status": "ok", "path": str(target)}


# -----------------------------------------------------------------------------
# 内部工具
# -----------------------------------------------------------------------------
def _is_admin() -> bool:
    """是否以管理员身份运行。

    用途：某些安装路径（Program Files）在非管理员下不可写，
    界面可据此给出更准确的提示，而不是让用户看到莫名的写入失败。
    """
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 探测失败不应影响任何主流程
        return False
