"""启动回归：缓存不能把 CLR 可导入误判为原生窗口可用。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from accountbook.shell.clr_runtime import _load_cache, _probe_command, _save_cache
from accountbook.shell.webview_runtime import _compatible_source


def test_folder_picker_adapter_does_not_execute_framework_private_types() -> None:
    code = _compatible_source(
        "before = 1\nclass OpenFolderDialog:\n    raise RuntimeError('private COM type')\n"
        "after = OpenFolderDialog\n", "winforms.py",
    )
    replacement = object()
    namespace = {"_accountbook_folder_dialog": replacement}
    exec(code, namespace)
    assert namespace["before"] == 1
    assert namespace["after"] is replacement


@pytest.mark.parametrize("source", ["pass", "class OpenFolderDialog: pass\nclass OpenFolderDialog: pass"])
def test_changed_upstream_structure_is_rejected(source: str) -> None:
    with pytest.raises(RuntimeError, match="结构已改变"):
        _compatible_source(source, "winforms.py")


@pytest.mark.parametrize("payload", [{"clr_runtime": "coreclr"}, [], {"schema": 1, "clr_runtime": "netfx"}])
def test_old_import_only_cache_is_invalid(tmp_path: Path, payload: object) -> None:
    cache = tmp_path / "runtime-cache.json"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert _load_cache(cache) is None


def test_verified_backend_cache_is_reused(tmp_path: Path) -> None:
    cache = tmp_path / "runtime-cache.json"
    _save_cache(cache, "coreclr")
    assert _load_cache(cache) == "coreclr"


@pytest.mark.parametrize("frozen", [False, True])
def test_both_build_forms_probe_the_application_backend(frozen: bool) -> None:
    with patch("accountbook.shell.clr_runtime.is_frozen", return_value=frozen):
        command = _probe_command("coreclr")
    assert command[-2:] == ["--probe-clr", "coreclr"]
    assert "-c" not in command
    if not frozen:
        assert command[1:3] == ["-m", "accountbook"]
