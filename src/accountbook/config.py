"""应用配置 —— 环境变量驱动的运行时设置 + 可持久化的用户偏好。

分层原则（避免"配置散落各处"这一常见腐化）::

    RuntimeSettings  只读、进程级、由环境变量注入（端口、dev 模式、日志级别…）
                     —— 不需要也不应该被用户在界面里修改。
    UserPreferences  可写、用户级、持久化到 <data>/config.json
                     —— 界面里能改的东西（主题、语言、窗口尺寸、隐私模式…）。

线程安全：
    后端服务运行在 uvicorn 的工作线程中，而配置变更可能由 API 请求触发、
    同时需要通知桌面外壳（例如主题变化要同步 Windows 标题栏）。因此
    :class:`ConfigStore` 内部使用可重入锁，并在变更后按注册顺序回调订阅者。

需求追溯：REQ-7（日/夜切换需持久化）、REQ-10（预留后续扩展字段）、REQ-14（配置与数据同处本地）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import AppPaths

__all__ = [
    "ConfigStore",
    "RuntimeSettings",
    "UserPreferences",
    "WindowState",
]

_logger = logging.getLogger(__name__)

#: 偏好文件的结构版本。未来若发生不兼容变更，用它驱动迁移函数。
#: 需求 10：预留后续开发 —— 迁移链比"直接覆盖"安全得多。
PREFS_SCHEMA_VERSION = 1


class RuntimeSettings(BaseSettings):
    """进程级运行时设置，全部可由 ``ACCOUNTBOOK_*`` 环境变量覆盖。

    这些值**不进入** config.json：它们描述"这一次怎么跑"，
    而不是"用户喜欢什么"。
    """

    model_config = SettingsConfigDict(
        env_prefix="ACCOUNTBOOK_",
        env_file=None,  # 刻意不读 .env：避免用户无意中把密钥提交进仓库
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行模式 -----------------------------------------------------------
    dev: bool = Field(
        default=False,
        description="开发模式：外壳加载 Vite 开发服务器，支持前端热更新。",
    )
    dev_server_url: str = Field(
        default="http://127.0.0.1:5173",
        description="Vite 开发服务器地址（仅 dev=True 时使用）。",
    )
    dev_backend_port: int = Field(
        default=8787,
        description=(
            "开发模式下后端固定端口。"
            "固定端口是为了让 Vite 的 proxy 配置可以静态指向它；"
            "生产模式一律使用 0（由操作系统分配随机空闲端口）。"
        ),
    )

    # ---- 日志 ---------------------------------------------------------------
    log_level: str = Field(default="INFO", description="DEBUG / INFO / WARNING / ERROR")
    log_to_console: bool = Field(default=True, description="是否同时输出到控制台（打包后建议关闭）")
    log_max_bytes: int = Field(default=2 * 1024 * 1024, description="单个日志文件上限")
    log_backup_count: int = Field(default=5, description="轮转保留的日志文件数量")

    # ---- 窗口 ---------------------------------------------------------------
    window_width: int = Field(default=1320, description="首次启动的窗口宽度（逻辑像素）")
    window_height: int = Field(default=880, description="首次启动的窗口高度")
    window_min_width: int = Field(default=1100, description="最小宽度：低于此值侧边栏与表格会挤压")
    window_min_height: int = Field(default=700, description="最小高度")

    # ---- 桌面外壳 -----------------------------------------------------------
    gui_backend: str | None = Field(
        default=None,
        description="pywebview 渲染后端；None 表示自动选择（Windows 上为 edgechromium）。",
    )
    webview_debug: bool = Field(default=False, description="打开 WebView2 开发者工具")
    window_backdrop: Literal["auto", "mica", "acrylic", "tabbed", "none"] = Field(
        default="auto",
        description="Windows 11 系统背景材质；auto 会在支持的版本上尝试 Mica，失败则无。",
    )
    enable_tray: bool = Field(
        default=True,
        description="是否创建系统托盘图标。P6 定时任务按时触发依赖托盘常驻。",
    )
    serve_only: bool = Field(
        default=False,
        description=(
            "只启动本地 HTTP 服务，不创建任何桌面外壳。用于端到端测试与截图验证，也方便偏好自带浏览器的用户。"
        ),
    )

    # ---- 安全与稳定性 -------------------------------------------------------
    single_instance: bool = Field(
        default=True,
        description="是否启用单实例锁。双开会造成 SQLite 写冲突与主题互相覆盖。",
    )
    shutdown_grace_seconds: float = Field(default=3.0, description="退出时等待后端收敛的秒数")


class WindowState(BaseModel):
    """窗口几何状态 —— 独立成模型便于未来支持多显示器记忆。"""

    width: int = 1320
    height: int = 880
    x: int | None = None
    y: int | None = None
    maximized: bool = False


class UserPreferences(BaseModel):
    """用户偏好 —— 持久化到 ``<data>/config.json``。

    设计要点
    --------
    * ``extra="allow"``：保留未知字段并原样写回。这样老版本程序读取新版本
      写出的配置时不会把用户的设置**擦掉**（需求 10：预留后续扩展）。
    * 每个字段带注释说明它对应哪条需求，便于审计。
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    schema_version: int = PREFS_SCHEMA_VERSION

    # REQ-7 日夜间切换：system = 跟随 Windows 个性化设置
    theme: Literal["light", "dark", "system"] = "system"
    # 需求取舍：中文为主 + i18n 框架预留英文
    language: Literal["zh-CN", "en-US"] = "zh-CN"
    # 无障碍：动画敏感人群可关闭全部非必要动效
    reduce_motion: bool = False
    # K 线/热力图配色：cn = 红涨绿跌（A 股习惯），intl = 绿涨红跌（国际习惯）
    money_color_scheme: Literal["cn", "intl"] = "cn"
    # 隐私模式：金额与商户名在界面上默认打码（适合演示与公共场合）
    privacy_mode: bool = False
    # 侧边栏折叠状态与顺序（前端读写；后端仅做持久化，不解释其内部结构）
    sidebar_collapsed: bool = False
    sidebar_order: list[str] = Field(default_factory=list)

    window: WindowState = Field(default_factory=WindowState)

    # 生命周期标记
    last_version: str = ""
    first_run_completed: bool = False


class ConfigStore:
    """偏好读写 + 变更订阅。

    写入策略：**原子替换**（先写临时文件，再 ``os.replace``）。
    直接覆写会导致断电/崩溃时 config.json 变成半截 JSON，
    进而让用户"每次启动都要重设主题"——这类问题极难排查，必须在源头杜绝。
    """

    def __init__(self, path: Path, defaults: UserPreferences | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._listeners: list[Callable[[UserPreferences], None]] = []
        self._prefs = self._load(defaults or UserPreferences())

    # ---- 读取 ---------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> UserPreferences:
        """返回**深拷贝**快照，防止调用方无意间修改内部状态。"""
        with self._lock:
            return self._prefs.model_copy(deep=True)

    # ---- 写入 ---------------------------------------------------------------
    def update(self, **changes: Any) -> UserPreferences:
        """局部更新偏好并落盘，随后通知订阅者。

        无效值不会导致崩溃：``ValidationError`` 会被记录并**忽略该字段**，
        保留其余有效变更。理由：配置文件被手改坏是常见场景，
        程序应当能自愈，而不是每次都弹一个看不懂的错误。
        """
        with self._lock:
            merged = self._prefs.model_dump()
            merged.update(changes)
            try:
                self._prefs = UserPreferences.model_validate(merged)
            except ValidationError as exc:
                _logger.warning("偏好设置包含非法字段，已忽略：%s", exc.errors())
                # 退回逐字段尝试，最大限度保留可用变更
                accepted = self._prefs.model_dump()
                for key, value in changes.items():
                    try:
                        probe = dict(accepted)
                        probe[key] = value
                        UserPreferences.model_validate(probe)
                        accepted[key] = value
                    except ValidationError:
                        _logger.warning("忽略非法偏好项：%s", key)
                self._prefs = UserPreferences.model_validate(accepted)
            self._save_locked()
            snapshot = self._prefs.model_copy(deep=True)

        self._notify(snapshot)
        return snapshot

    def set_window_state(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        x: int | None = None,
        y: int | None = None,
        maximized: bool | None = None,
    ) -> None:
        """更新窗口几何（关闭窗口时调用一次，避免拖动过程中频繁落盘）。"""
        with self._lock:
            current = self._prefs.window.model_dump()
            for key, value in (
                ("width", width),
                ("height", height),
                ("x", x),
                ("y", y),
                ("maximized", maximized),
            ):
                if value is not None:
                    current[key] = value
            self._prefs.window = WindowState.model_validate(current)
            self._save_locked()

    # ---- 订阅 ---------------------------------------------------------------
    def subscribe(self, listener: Callable[[UserPreferences], None]) -> Callable[[], None]:
        """注册变更监听器，返回取消订阅函数。

        典型订阅者：桌面外壳（主题变化 → 同步 Windows 标题栏深色状态）。
        """
        with self._lock:
            self._listeners.append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsubscribe

    def _notify(self, prefs: UserPreferences) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(prefs)
            except Exception:
                _logger.exception("偏好变更监听器执行失败（已忽略）")

    # ---- 内部：加载与保存 ---------------------------------------------------
    def _load(self, defaults: UserPreferences) -> UserPreferences:
        if not self._path.exists():
            return defaults
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("配置文件无法解析，将使用默认值：%s", exc)
            return defaults

        raw = _migrate(raw)
        try:
            return UserPreferences.model_validate(raw)
        except ValidationError as exc:
            _logger.warning("配置文件结构不合法，将使用默认值：%s", exc.errors())
            return defaults

    def _save_locked(self) -> None:
        """调用方必须已持有 ``self._lock``。"""
        payload = self._prefs.model_dump(mode="json")
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)  # 原子替换：要么完整新内容，要么完整旧内容
        except OSError as exc:
            _logger.error("偏好设置写入失败（本次变更仅在内存生效）：%s", exc)


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """配置结构迁移入口。

    当前仅有一个版本，因此只是补全缺失的 ``schema_version``。
    P1 起若结构变更，在此按版本号逐级升级，切勿直接丢弃用户配置。
    """
    if "schema_version" not in raw:
        raw["schema_version"] = PREFS_SCHEMA_VERSION
    return raw


def build_config_store(paths: AppPaths) -> ConfigStore:
    """按路径解析结果构造配置仓库（便于测试注入临时目录）。"""
    return ConfigStore(paths.config_file)
