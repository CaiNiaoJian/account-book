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

from . import APP_ID

__all__ = [
    "AppPaths",
    "DataDirCandidate",
    "data_dir_candidates",
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


def _probe_writable(directory: Path) -> tuple[bool, str]:
    """探测目录是否**真的**可写，并返回失败原因。

    为什么不能只用 ``os.access``：在 Windows 上它只反映只读属性，
    完全看不到 ACL 与 AppContainer 之类的访问限制。因此这里真写一个临时文件再删除 ——
    这是最诚实的判定，也是唯一能提前发现"目录存在但写不进去"的办法。

    返回 ``(是否可写, 原因)``。原因用于写入日志与错误对话框，
    让用户看到的是"哪个路径因为什么不可用"，而不是一句笼统的失败。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建目录（{exc.__class__.__name__}: {exc}）"

    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ab_write_probe_", delete=True):
            pass
    except OSError as exc:
        return False, f"目录存在但不可写入（{exc.__class__.__name__}: {exc}）"
    return True, "可写"


@dataclass(frozen=True, slots=True)
class DataDirCandidate:
    """一个候选数据目录及其来源说明。

    「来源」是给用户看的：当他发现账本没在预期位置时，
    需要一眼看懂"为什么放到了这里"，而不是去读日志。
    """

    path: Path
    source: str
    portable: bool = False


def data_dir_candidates(program_root: Path, *, portable: bool) -> list[DataDirCandidate]:
    """按优先级给出候选数据目录列表（不探测，只排序）。

    排序原则（谨慎且可解释）：

    * **便携模式**：数据要跟着程序走，首选程序同级 ``data/``；
      不可写（例如解压到了 Program Files 或只读介质）再逐级回退。
    * **安装模式**：首选 ``%LOCALAPPDATA%``（本机数据语义，不漫游）；
      之后依次尝试 ``%APPDATA%``、用户文档目录、程序同级 ``data/``，
      最后才是系统临时目录。

    把临时目录放在最后是有意的：它可能被系统清理，账本放在那里只是"总比打不开强"。
    """
    candidates: list[DataDirCandidate] = []
    seen: set[Path] = set()

    def add(path: Path | None, source: str, *, is_portable: bool = False) -> None:
        if path is None:
            return
        resolved = Path(path)
        # 去重：不同来源可能指向同一目录（例如把程序放在用户目录下）
        key = str(resolved).rstrip("\\/").lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(DataDirCandidate(path=resolved, source=source, portable=is_portable))

    roaming_appdata = os.environ.get("APPDATA")
    user_profile = os.environ.get("USERPROFILE") or str(Path.home())

    if portable:
        add(program_root / "data", "便携模式：程序目录下的 data", is_portable=True)

    add(_default_local_appdata(), "用户本地数据目录（%LOCALAPPDATA%）")
    add(Path(roaming_appdata) / APP_ID if roaming_appdata else None, "用户漫游数据目录（%APPDATA%）")
    add(Path(user_profile) / "Documents" / APP_ID, "用户文档目录")
    if not portable:
        add(program_root / "data", "程序目录下的 data", is_portable=True)
    add(Path(tempfile.gettempdir()) / APP_ID, "系统临时目录（可能被系统清理）")

    return candidates


def _default_local_appdata() -> Path:
    """安装版首选数据目录：``%LOCALAPPDATA%\\AccountBook``。

    保留为独立函数，是为了让 :func:`data_dir_candidates` 与文档
    都能明确引用"首选位置"这一概念，而不必在代码里重复拼路径。
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        # 极端降级：非 Windows 或环境变量缺失时落在用户主目录
        base = str(Path.home() / ".local" / "share")
    return Path(base) / APP_ID


@dataclass(frozen=True, slots=True)
class AppPaths:
    """解析完成的路径集合（不可变，避免运行期被意外改写）。"""

    program_root: Path  #: 程序根目录（exe / 仓库根）
    data: Path  #: 用户数据根目录 —— 隐私隔离的核心
    resources: Path  #: 只读资源根目录
    portable: bool  #: 是否为便携模式
    frozen: bool  #: 是否为打包运行
    degraded: bool = False  #: 是否未使用首选数据目录（含便携降级与多级回退）
    #: 当前数据目录的来源说明（用于日志、诊断与界面提示）
    data_dir_source: str = ""
    #: 被跳过/失败的候选目录及原因（"路径 — 来源：原因"），供排障使用
    data_dir_attempts: tuple[str, ...] = ()

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
            "data_dir_source": self.data_dir_source,
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

    数据目录的选择遵循「**逐个探测、真实写入、明确告知**」三步：

    1. 按 :func:`data_dir_candidates` 的优先级依次尝试候选目录；
    2. 每个候选都做**真实写入探测**（``_probe_writable``），
       只有确认可写才采用 —— 避免"目录存在但写不进去"这种最隐蔽的故障；
    3. 记录最终选用的来源与被跳过的候选及原因，供日志、诊断与界面显示。

    为什么不能简单地"首选不可用就报错退出"：
    用户双击一个程序却只看到一个失败对话框，是最糟糕的体验；
    而账本这类数据放到第二、第三备选位置，用户几乎没有损失。

    唯一的例外是**显式指定**的数据目录（``--data-dir`` / ``ACCOUNTBOOK_DATA_DIR``）：
    用户已经明确表达了意图，此时不做任何回退 —— 悄悄换个位置存放账本，
    比直接报错更不可接受。
    """
    global _cached
    if _cached is not None:
        return _cached

    program_root = _program_root()
    resources = resource_root()
    portable_flag = is_portable()

    env_data = os.environ.get("ACCOUNTBOOK_DATA_DIR")
    attempts: list[str] = []

    if env_data:
        # 显式覆盖：完全信任调用方（测试、多档案场景），不探测、不回退
        data = Path(env_data).expanduser().resolve()
        source = "显式指定（--data-dir / ACCOUNTBOOK_DATA_DIR）"
        degraded = False
        portable = False
    else:
        candidates = data_dir_candidates(program_root, portable=portable_flag)
        chosen: DataDirCandidate | None = None
        for index, candidate in enumerate(candidates):
            writable, reason = _probe_writable(candidate.path)
            if writable:
                chosen = candidate
                if index > 0:
                    _logger.warning(
                        "首选数据目录不可用，已改用第 %d 个候选：%s（%s）",
                        index + 1,
                        candidate.path,
                        candidate.source,
                    )
                break
            attempts.append(f"{candidate.path} — {candidate.source}：{reason}")
            _logger.info("候选数据目录不可用：%s（%s）", candidate.path, reason)

        if chosen is None:
            # 全部候选都不可写：仍返回首选路径，让 ensure() 抛出可读错误，
            # 由 app.py 汇总所有尝试记录后给出可操作的提示。
            chosen = candidates[0]
            _logger.error("所有候选数据目录均不可写，已尝试 %d 个位置", len(attempts))

        data = chosen.path
        source = chosen.source
        degraded = chosen is not candidates[0]
        portable = chosen.portable

    _cached = AppPaths(
        program_root=program_root,
        data=data,
        resources=resources,
        portable=portable,
        frozen=is_frozen(),
        degraded=degraded,
        data_dir_source=source,
        data_dir_attempts=tuple(attempts),
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
