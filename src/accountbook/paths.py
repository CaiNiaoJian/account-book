"""路径解析 —— 安装版 / 便携版 双模式数据目录（需求 1、需求 14）。

为什么单独成模块：
    * 「数据放哪里」是本项目**最重要的一条隐私边界**（需求 14：本地数据库不上云）。
      把判定逻辑集中在这里，才能保证没有任何模块私自拼路径、把数据写到意外位置。
    * 打包成 zip 便携版后，程序可能被解压到 U 盘或只读目录，必须能优雅降级。

两种运行模式
------------
1. **安装版**（Inno Setup 安装到 Program Files 等位置）
   数据目录 = ``%LOCALAPPDATA%\\AccountBook``
   理由：安装目录通常不可写，且卸载时不应连带删除用户账本。

2. **便携版**（zip 解压即用）
   数据目录 = ``<exe 所在目录>/data``，由打包脚本放置的 ``portable.flag`` 触发。
   理由：U 盘随身携带时，账本要跟着走。
   若该目录不可写（例如被解压到 Program Files），**自动降级**为安装版路径并记录警告。

环境变量覆盖（便于测试与多档案隔离）
------------------------------------
``ACCOUNTBOOK_DATA_DIR``  覆盖数据目录
``ACCOUNTBOOK_PORTABLE``  取值为 1/true/yes 时强制便携模式
``ACCOUNTBOOK_HOME``      覆盖"程序根目录"（主要用于测试）

需求追溯：REQ-1（双形态交付）、REQ-14（数据隔离）
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AppPaths",
    "get_paths",
    "is_frozen",
    "is_portable",
    "reset_paths_cache",
]

_logger = logging.getLogger(__name__)

#: 便携模式标记文件名 —— 打包脚本会把它放进便携版 zip 的根目录
PORTABLE_FLAG_NAME = "portable.flag"

#: 数据目录下的子目录清单。集中声明便于：
#:   1) 首次启动一次性创建；
#:   2) .gitignore 与本清单一一对应地复核（需求 14）。
DATA_SUBDIRS: tuple[str, ...] = (
    "attachments",  # 流水附件（发票、小票、工资条图片）
    "attachments/cards",  # 用户自定义卡面（P2 资产卡片墙）
    "backups",  # 自动与手动备份（*.abk 加密备份亦落此处）
    "logs",  # 轮转日志与崩溃转储
    "plugins",  # 用户安装的插件实例（源码模板在包内 plugins_sdk/）
    "models",  # 机器学习模型产物（P7+ 预留）
    "exports",  # 用户导出的报表 / CSV / PNG
    "cache",  # 可重建的缓存（图表聚合、缩略图），删除不影响数据完整性
    "webview",  # WebView2 的用户数据（localStorage / Cookie 持久化）
)


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 frozen 环境中。

    PyInstaller 会注入 ``sys.frozen``；``sys._MEIPASS`` 则是解包后的临时根目录
    （onefile 模式）或 _internal 目录（onedir 模式）。
    """
    return bool(getattr(sys, "frozen", False))


def _program_root() -> Path:
    """返回「程序根目录」，即 exe 或仓库根所在位置。

    * frozen：exe 所在目录（便携版的判定基准）
    * 源码运行：仓库根目录（``src/accountbook/paths.py`` 向上三级）
    """
    override = os.environ.get("ACCOUNTBOOK_HOME")
    if override:
        return Path(override).expanduser().resolve()

    if is_frozen():
        return Path(sys.executable).resolve().parent

    # paths.py → accountbook → src → <repo root>
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """返回随包分发的**只读资源**根目录（图标、字体、前端构建产物）。

    * frozen：PyInstaller 解包目录下的 ``accountbook_resources``（见 .spec 的 datas 映射）
    * 源码运行：``src/accountbook/resources``
    """
    if is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return meipass / "accountbook_resources"
    return Path(__file__).resolve().parent / "resources"


def is_portable() -> bool:
    """是否处于便携模式。

    判定顺序：
        1. 环境变量 ``ACCOUNTBOOK_PORTABLE`` 显式指定（测试与高级用法）
        2. 程序根目录存在 ``portable.flag``
    """
    flag_env = os.environ.get("ACCOUNTBOOK_PORTABLE", "").strip().lower()
    if flag_env in {"1", "true", "yes", "on"}:
        return True
    return (_program_root() / PORTABLE_FLAG_NAME).exists()


def _is_writable(directory: Path) -> bool:
    """探测目录是否真的可写。

    注意：仅用 ``os.access`` 在 Windows 上不可靠（ACL 与只读属性行为不同），
    因此采用「真写一个临时文件再删除」的方式，这是最诚实的判定。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ab_write_probe_", delete=True):
            pass
        return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class AppPaths:
    """解析完成的路径集合（不可变，避免运行期被意外改写）。"""

    program_root: Path  #: 程序根目录（exe / 仓库根）
    data: Path  #: 用户数据根目录 —— 隐私隔离的核心
    resources: Path  #: 只读资源根目录
    portable: bool  #: 是否为便携模式
    frozen: bool  #: 是否为打包运行
    degraded: bool = False  #: 便携模式是否因不可写而降级

    # ---- 只读资源 -----------------------------------------------------------
    @property
    def web_dist(self) -> Path:
        """前端构建产物目录（由 vite build 输出到 resources/web_dist）。"""
        return self.resources / "web_dist"

    @property
    def web_index(self) -> Path:
        return self.web_dist / "index.html"

    @property
    def icons(self) -> Path:
        return self.resources / "icons"

    @property
    def app_icon(self) -> Path:
        """应用图标（.ico）。缺失时上层必须优雅降级，不得崩溃。"""
        return self.icons / "app.ico"

    # ---- 用户数据 -----------------------------------------------------------
    @property
    def database(self) -> Path:
        """SQLite 主库（P1 起使用）。"""
        return self.data / "accountbook.db"

    @property
    def config_file(self) -> Path:
        return self.data / "config.json"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "accountbook.log"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def attachments(self) -> Path:
        return self.data / "attachments"

    @property
    def plugins(self) -> Path:
        return self.data / "plugins"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def exports(self) -> Path:
        return self.data / "exports"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def webview_storage(self) -> Path:
        """WebView2 用户数据目录 —— 主题、语言等前端偏好靠它持久化。

        若不指定（或使用 private_mode），localStorage 在重启后会清空，
        「日/夜主题记住上次选择」这类需求将无法实现。
        """
        return self.data / "webview"

    @property
    def lock_file(self) -> Path:
        """单实例锁文件。"""
        return self.data / "app.lock"

    @property
    def runtime_cache_file(self) -> Path:
        """运行环境探测结果的缓存（当前用于 CLR 运行时选择）。

        与「用户偏好」分开存放的理由：这是**机器事实**而非用户选择，
        用户重置偏好时不该把它一起丢掉；反之，用户想重新探测
        （例如刚装好 .NET 运行时）只需删掉这个文件。
        """
        return self.data / "runtime-cache.json"

    # ---- 行为 ---------------------------------------------------------------
    def ensure(self) -> None:
        """创建全部必需目录。幂等，可在启动流程中安全多次调用。"""
        for name in DATA_SUBDIRS:
            (self.data / name).mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """供日志与「关于」页展示的路径摘要（绝不包含用户数据内容）。"""
        return {
            "program_root": str(self.program_root),
            "data_dir": str(self.data),
            "resources": str(self.resources),
            "portable": self.portable,
            "frozen": self.frozen,
            "degraded": self.degraded,
            "database": str(self.database),
            "log_file": str(self.log_file),
        }


# 进程内缓存：路径在生命周期内不会变化，但解析涉及磁盘探测（写探针），不宜反复执行
_cached: AppPaths | None = None


def reset_paths_cache() -> None:
    """清空缓存 —— 仅供测试使用（生产代码不应调用）。"""
    global _cached
    _cached = None


def get_paths() -> AppPaths:
    """解析并缓存当前进程的路径集合。

    便携模式若判定为不可写，会**自动降级**到 ``%LOCALAPPDATA%\\AccountBook``，
    并置 ``degraded=True``。这是"谨慎"原则的体现：宁可用系统目录，
    也不要在 U 盘拔出后写出半截数据库。
    """
    global _cached
    if _cached is not None:
        return _cached

    program_root = _program_root()
    resources = resource_root()
    portable = is_portable()
    degraded = False

    env_data = os.environ.get("ACCOUNTBOOK_DATA_DIR")
    if env_data:
        # 显式覆盖：完全信任调用方（测试、多档案场景），不做降级判断
        data = Path(env_data).expanduser().resolve()
    elif portable:
        candidate = program_root / "data"
        if _is_writable(candidate):
            data = candidate
        else:
            degraded = True
            data = _default_local_appdata()
            _logger.warning(
                "便携模式目标目录不可写，已降级到用户数据目录：%s → %s",
                candidate,
                data,
            )
    else:
        data = _default_local_appdata()

    _cached = AppPaths(
        program_root=program_root,
        data=data,
        resources=resources,
        portable=portable and not degraded,
        frozen=is_frozen(),
        degraded=degraded,
    )
    return _cached


def _default_local_appdata() -> Path:
    """安装版默认数据目录：``%LOCALAPPDATA%\\AccountBook``。

    选择 LOCALAPPDATA 而非 APPDATA 的理由：账本与附件体积较大，
    且属于"本机数据"语义，不应漫游到域账户的服务器配置文件里。
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        # 极端降级：非 Windows 或环境变量缺失时落在用户主目录
        base = str(Path.home() / ".local" / "share")
    from . import APP_ID  # 延迟导入以避免循环依赖

    return Path(base) / APP_ID
