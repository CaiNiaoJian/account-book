"""路径解析用例 —— 覆盖安装版 / 便携版双模式与降级逻辑（REQ-1、REQ-14）。

这些用例的价值在于**防止数据写到意外位置**。一旦有人改动路径逻辑，
这里会立刻失败，而不是等到用户的账本出现在某个奇怪目录里才被发现。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from accountbook.paths import (
    DATA_SUBDIRS,
    PORTABLE_FLAG_NAME,
    AppPaths,
    get_paths,
    is_frozen,
    reset_paths_cache,
)


class TestDataDirResolution:
    """数据目录解析。"""

    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式环境变量优先级最高，且不做任何降级判断。"""
        target = tmp_path / "custom-ledger"
        monkeypatch.setenv("ACCOUNTBOOK_DATA_DIR", str(target))
        reset_paths_cache()

        paths = get_paths()
        assert paths.data == target.resolve()
        assert paths.degraded is False

    def test_portable_mode_uses_program_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """根目录存在 portable.flag 时，数据应落在程序同级 data/。"""
        program_root = tmp_path / "portable-app"
        program_root.mkdir()
        (program_root / PORTABLE_FLAG_NAME).write_text("", encoding="utf-8")
        monkeypatch.delenv("ACCOUNTBOOK_DATA_DIR", raising=False)
        monkeypatch.setenv("ACCOUNTBOOK_HOME", str(program_root))
        reset_paths_cache()

        paths = get_paths()
        assert paths.portable is True
        assert paths.data == program_root / "data"

    def test_non_portable_uses_local_appdata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 portable.flag 时使用 %LOCALAPPDATA%\\AccountBook。"""
        local = tmp_path / "localappdata"
        monkeypatch.delenv("ACCOUNTBOOK_DATA_DIR", raising=False)
        monkeypatch.setenv("ACCOUNTBOOK_LOCALAPPDATA_PLACEHOLDER", "1")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("ACCOUNTBOOK_HOME", str(tmp_path / "app"))
        reset_paths_cache()

        paths = get_paths()
        assert paths.portable is False
        assert paths.data == local / "AccountBook"

    def test_portable_falls_back_when_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """便携目录不可写时降级到用户数据目录，并标记 degraded（谨慎原则）。

        这里用"把 data 路径占位成一个文件"来制造必然的写入失败，
        比依赖真实权限位更稳定（也适用于以管理员身份跑测试的场景）。
        """
        program_root = tmp_path / "readonly-portable"
        program_root.mkdir()
        (program_root / PORTABLE_FLAG_NAME).write_text("", encoding="utf-8")
        # 制造冲突：名为 data 的**文件**会阻止同名目录被创建
        (program_root / "data").write_text("occupied", encoding="utf-8")

        local = tmp_path / "localappdata"
        monkeypatch.delenv("ACCOUNTBOOK_DATA_DIR", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("ACCOUNTBOOK_HOME", str(program_root))
        reset_paths_cache()

        paths = get_paths()
        assert paths.degraded is True
        assert paths.portable is False
        assert paths.data == local / "AccountBook"

    def test_ensure_creates_all_subdirs(self, app_paths: AppPaths) -> None:
        """``ensure()`` 必须一次性创建全部子目录（幂等）。"""
        app_paths.ensure()  # 第二次调用不应抛异常
        for name in DATA_SUBDIRS:
            assert (app_paths.data / name).is_dir(), f"缺少子目录：{name}"


class TestAppPathsProperties:
    """派生路径属性。"""

    def test_derived_paths_are_under_data_dir(self, app_paths: AppPaths) -> None:
        """所有用户数据路径都必须位于 data/ 之下（便于 .gitignore 与备份）。"""
        for candidate in (
            app_paths.database,
            app_paths.config_file,
            app_paths.logs,
            app_paths.backups,
            app_paths.attachments,
            app_paths.plugins,
            app_paths.models,
            app_paths.exports,
            app_paths.cache,
            app_paths.webview_storage,
            app_paths.lock_file,
        ):
            assert app_paths.data in candidate.parents or candidate.parent == app_paths.data

    def test_web_assets_live_under_resources(self, app_paths: AppPaths) -> None:
        """前端产物与只读资源必须在 resources 下，绝不与用户数据混放。"""
        assert app_paths.resources in app_paths.web_index.parents
        assert app_paths.resources in app_paths.app_icon.parents

    def test_describe_contains_no_user_content(self, app_paths: AppPaths) -> None:
        """``describe()`` 只输出路径字符串，便于安全地写入日志与「关于」页。"""
        described = app_paths.describe()
        assert set(described) == {
            "program_root",
            "data_dir",
            "resources",
            "portable",
            "frozen",
            "degraded",
            "database",
            "log_file",
        }
        assert all(
            isinstance(value, str)
            for key, value in described.items()
            if key not in {"portable", "frozen", "degraded"}
        )
        assert described["portable"] in {True, False}


class TestHelpers:
    """工具函数。"""

    def test_is_frozen_false_in_source_run(self) -> None:
        """源码运行不应被判定为打包环境。"""
        assert is_frozen() is False

    def test_dataclass_is_frozen(self, app_paths: AppPaths) -> None:
        """AppPaths 不可变：防止运行期被某处意外改写数据目录。"""
        with pytest.raises(dataclasses.FrozenInstanceError):
            app_paths.data = Path("D:/elsewhere")  # type: ignore[misc]
