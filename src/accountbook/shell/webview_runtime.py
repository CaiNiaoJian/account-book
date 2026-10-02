"""pywebview 6.2.1 的 CoreCLR 兼容适配，保持系统安全策略不变。

上游附带的是 net462 WebView2 组件，且文件夹选择器依赖 Framework 私有类型。
CoreCLR 必须选用官方同版本的 netcoreapp3.0 组件，并使用公开 FolderBrowserDialog。
只替换这一处类定义；其余窗口代码仍来自已安装、固定版本的 pywebview。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

_MANAGED_DLLS = frozenset({"Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"})
_BACKEND_NAME = "webview.platforms.winforms"


class _FolderDialog:
    """使用 WinForms 公共 API，不依赖不同 .NET 版本间变化的私有 COM 类型。"""

    @classmethod
    def show(
        cls, parent: Any = None, initialDirectory: str | None = None,
        allow_multiple: bool = False, title: str | None = None,
    ) -> tuple[str, ...] | None:
        import System.Windows.Forms as forms

        dialog = forms.FolderBrowserDialog()
        try:
            dialog.SelectedPath = initialDirectory or ""
            dialog.Description = title or "选择文件夹"
            dialog.UseDescriptionForTitle = True
            if allow_multiple:
                if not hasattr(dialog, "Multiselect"):
                    raise NotImplementedError("当前 .NET 桌面运行时不支持多选文件夹")
                dialog.Multiselect = True
            result = dialog.ShowDialog(parent) if parent is not None else dialog.ShowDialog()
            if result != forms.DialogResult.OK:
                return None
            if allow_multiple:
                return tuple(dialog.SelectedPaths)
            return (str(dialog.SelectedPath),)
        finally:
            dialog.Dispose()


def _compatible_source(source: str, filename: str) -> Any:
    """仅改写指定类；找不到预期结构时明确失败，避免悄悄加载未经适配的版本。"""
    tree = ast.parse(source, filename=filename)
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OpenFolderDialog"]
    if len(matches) != 1:
        raise RuntimeError("pywebview 窗口后端结构已改变，需要更新 CoreCLR 适配")
    replacement = ast.Assign(
        targets=[ast.Name(id="OpenFolderDialog", ctx=ast.Store())],
        value=ast.Name(id="_accountbook_folder_dialog", ctx=ast.Load()),
    )
    tree.body[tree.body.index(matches[0])] = ast.copy_location(replacement, matches[0])
    return compile(ast.fix_missing_locations(tree), filename, "exec")


def prepare_webview(resources: Path) -> None:
    """在导入窗口后端前应用适配；源码、探测子进程、打包版共用此入口。"""
    import clr

    if os.environ.get("PYTHONNET_RUNTIME") != "coreclr":
        return
    if _BACKEND_NAME in sys.modules:
        return

    import webview
    import webview.util

    folder = resources / "clr" / "webview2"
    for name in _MANAGED_DLLS:
        if not (folder / name).is_file():
            raise FileNotFoundError(f"缺少 CoreCLR WebView2 组件：{folder / name}")

    original = webview.util.interop_dll_path

    def resolve_dll(name: str) -> str:
        return str(folder / name) if name in _MANAGED_DLLS else original(name)

    # 仅指定的两份官方组件走替代路径；原生 loader 与其他组件保持上游路径。
    webview.util.interop_dll_path = resolve_dll
    try:
        clr.AddReference("Microsoft.Win32.SystemEvents")
        source_path = Path(webview.__file__).parent / "platforms" / "winforms.py"
        if not source_path.is_file():
            # PyInstaller 的 PYZ 没有源文件；构建时显式收集固定依赖的原始源码。
            source_path = resources / "clr" / "pywebview" / "winforms.py"
        code = _compatible_source(source_path.read_text(encoding="utf-8"), str(source_path))
        spec = importlib.util.find_spec(_BACKEND_NAME)
        if spec is None:
            raise ImportError("未找到 pywebview Windows 后端")
        module = importlib.util.module_from_spec(spec)
        module.__dict__["_accountbook_folder_dialog"] = _FolderDialog
        sys.modules[_BACKEND_NAME] = module
        try:
            exec(code, module.__dict__)
        except BaseException:
            sys.modules.pop(_BACKEND_NAME, None)
            raise
    except BaseException:
        webview.util.interop_dll_path = original
        raise
