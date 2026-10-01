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
    data_dir_candidates,
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
        # 必须留下"试过哪里、为什么失败"的记录，否则排障只能靠猜
        assert paths.data_dir_attempts
        assert any(str(program_root / "data") in item for item in paths.data_dir_attempts)
        assert "LOCALAPPDATA" in paths.data_dir_source

    def test_install_mode_falls_back_through_candidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """安装版：首选 %LOCALAPPDATA% 不可写时，应继续尝试后续候选位置。

        这是"应用不该因为一个目录不可写就打不开"的核心保障。
        """
        local = tmp_path / "localappdata"
        local.mkdir()
        # 首选位置被一个同名**文件**占住 → 必然不可写
        (local / "AccountBook").write_text("occupied", encoding="utf-8")

        roaming = tmp_path / "roaming"
        profile = tmp_path / "profile"
        program_root = tmp_path / "app"
        program_root.mkdir()

        monkeypatch.delenv("ACCOUNTBOOK_DATA_DIR", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.setenv("USERPROFILE", str(profile))
        monkeypatch.setenv("ACCOUNTBOOK_HOME", str(program_root))
        reset_paths_cache()

        paths = get_paths()
        assert paths.data == roaming / "AccountBook", "应回退到 %APPDATA% 候选"
        assert paths.degraded is True
        assert paths.data_dir_attempts, "被跳过的首选位置必须留痕"

    def test_install_mode_prefers_local_appdata_when_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """首选位置可用时不得降级 —— 否则就是凭空改变用户的数据位置。"""
        local = tmp_path / "localappdata"
        monkeypatch.delenv("ACCOUNTBOOK_DATA_DIR", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("ACCOUNTBOOK_HOME", str(tmp_path / "app"))
        reset_paths_cache()

        paths = get_paths()
        assert paths.data == local / "AccountBook"
        assert paths.degraded is False
        assert paths.data_dir_attempts == ()

    def test_candidates_are_ordered_and_deduplicated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候选顺序必须稳定：便携优先程序目录，安装优先 LOCALAPPDATA。

        同时验证去重：不同来源指向同一目录时只保留一次。
        """
        profile = tmp_path / "profile"
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        monkeypatch.setenv("USERPROFILE", str(profile))

        install_candidates = data_dir_candidates(tmp_path / "app", portable=False)
        assert install_candidates[0].path == tmp_path / "local" / "AccountBook"
        assert install_candidates[-1].path.name == "AccountBook"
        assert len({str(c.path).lower() for c in install_candidates}) == len(install_candidates)

        portable_candidates = data_dir_candidates(tmp_path / "app", portable=True)
        assert portable_candidates[0].path == tmp_path / "app" / "data"
        assert portable_candidates[0].portable is True

    def test_explicit_data_dir_is_never_overridden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式指定的数据目录即使不可写也不回退 —— 尊重用户意图。

        悄悄把账本存到别处，比明确报错更不可接受。
        """
        explicit = tmp_path / "not-writable"
        explicit.write_text("occupied-by-file", encoding="utf-8")  # 同名文件占位
        monkeypatch.setenv("ACCOUNTBOOK_DATA_DIR", str(explicit))
        reset_paths_cache()

        paths = get_paths()
        assert paths.data == explicit.resolve()
        assert paths.degraded is False
        assert "显式指定" in paths.data_dir_source

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
            "data_dir_source",
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
        # 来源说明必须始终有值：日志与界面都依赖它解释"数据放在哪、为什么"
        assert described["data_dir_source"]


class TestHelpers:
    """工具函数。"""

    def test_is_frozen_false_in_source_run(self) -> None:
        """源码运行不应被判定为打包环境。"""
        assert is_frozen() is False

    def test_dataclass_is_frozen(self, app_paths: AppPaths) -> None:
        """AppPaths 不可变：防止运行期被某处意外改写数据目录。"""
        with pytest.raises(dataclasses.FrozenInstanceError):
            app_paths.data = Path("D:/elsewhere")  # type: ignore[misc]
