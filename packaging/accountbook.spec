# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller 打包配置（onedir 模式）
# -----------------------------------------------------------------------------
# 为什么选择 **onedir** 而不是 onefile：
#   * onefile 每次启动都要把上百 MB 解压到临时目录，冷启动常达数秒，
#     对"随手记一笔"的工具是致命的体验问题；
#   * onedir 便于把"插件目录 / 示例数据 / 许可证文件"放在程序目录旁，
#     用户与支持人员能直接看到，而不是藏在一个可执行文件里；
#   * 便携版 zip 的分发形态本来就是"一个目录"，onedir 天然契合。
#
# 关键点
#   * resources 被映射到 ``accountbook_resources``，与
#     ``accountbook/paths.py::resource_root()`` 的 frozen 分支严格对应。
#   * hiddenimports 覆盖 pywebview 的运行时后端与 uvicorn 的动态子模块 ——
#     它们都是"运行期按需导入"，静态分析看不到，漏掉就会出现
#     "开发能跑、打包后启动即崩"的经典问题。
#   * console=False 生成 GUI 程序（无黑色控制台窗口）；
#     为此 app.py 中的输出都经过 _emit() 保护，stdout 为 None 时不会崩溃。
# =============================================================================

from pathlib import Path
from importlib.util import find_spec
from importlib.metadata import version
import os

# SPECPATH 由 PyInstaller 注入，指向本文件所在目录（packaging/）
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / 'src'
RESOURCES = SRC / 'accountbook' / 'resources'
ENTRY = ROOT / 'packaging' / 'entry.py'
ICON = RESOURCES / 'icons' / 'app.ico'
VERSION_FILE = ROOT / 'packaging' / 'version_info.txt'

# CoreCLR 使用公开文件夹选择器适配上游后端，冻结后也需要原始源码。
webview_spec = find_spec('webview')
if webview_spec is None or webview_spec.origin is None:
    raise RuntimeError('缺少 pywebview，无法构建原生窗口')
WEBVIEW_ROOT = Path(webview_spec.origin).parent
if version('pywebview') != '6.2.1':
    raise RuntimeError('CoreCLR 适配要求 pywebview 6.2.1；升级依赖时需一起验证窗口后端')

# 诊断开关：windowed（console=False）构建里 stdout 不可见，
# 一旦启动阶段出问题会弹出一个需要人工点击的错误框，在自动化环境里表现为"卡死"。
# 需要排查启动问题时，用：
#     $env:ACCOUNTBOOK_BUILD_CONSOLE = '1'; 重新打包
# 即可得到一个能看到完整 traceback 的控制台版本。发布构建不要设置该变量。
CONSOLE = os.environ.get('ACCOUNTBOOK_BUILD_CONSOLE', '0') == '1'

# -----------------------------------------------------------------------------
# 运行期动态导入的模块：必须显式声明，静态分析无法发现
# -----------------------------------------------------------------------------
hidden_imports = [
    # --- pywebview 的 Windows 后端链 ---------------------------------------
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr_loader',
    'pythonnet',
    'clr',
    # --- uvicorn：全部子模块都是按字符串动态加载 ---------------------------
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    # --- 托盘：pystray 按平台选择后端 --------------------------------------
    'pystray._win32',
    # --- 邮件/编码等边缘依赖（FastAPI/Starlette 的间接引用） ----------------
    'email.mime.multipart',
    'email.mime.text',
]

# -----------------------------------------------------------------------------
# 明确排除：避免把开发机上的科学计算栈卷进产物（体积从数百 MB 降到数十 MB）
# -----------------------------------------------------------------------------
excludes = [
    'tkinter',
    'matplotlib',
    'numpy',
    'pandas',
    'geopandas',
    'scipy',
    'sklearn',
    'PyQt5',
    'PyQt6',
    'PySide2',
    'PySide6',
    'IPython',
    'jupyter',
    'pytest',
    'ruff',
    'mypy',
    'setuptools',
    'pip',
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    # 两类数据必须显式打包：
    #   1. 只读资源（图标/字体/前端产物）→ _MEIPASS/accountbook_resources，
    #      与 paths.py 的 frozen 分支约定一致；
    #   2. **Alembic 迁移脚本** → 保持与源码相同的包内相对路径。
    #      它们不是被 import 的模块，PyInstaller 的依赖分析看不见它们；
    #      漏掉的话，打包版启动时会报
    #      "Path doesn't exist: .../db/migrations" —— 而源码运行一切正常，
    #      属于只有真打包一次才会暴露的问题（已实际踩到）。
    #      注意 versions/ 里的迁移文件也必须是**数据**：
    #      将来新增迁移时无需改这里（整个目录一起打包）。
    datas=[
        (str(RESOURCES), 'accountbook_resources'),
        (str(WEBVIEW_ROOT / 'platforms' / 'winforms.py'), 'accountbook_resources/clr/pywebview'),
        (str(WEBVIEW_ROOT.parent / 'pywebview-6.2.1.dist-info' / 'licenses' / 'LICENSE'),
         'accountbook_resources/clr/pywebview'),
        (str(SRC / 'accountbook' / 'db' / 'migrations'), 'accountbook/db/migrations'),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AccountBook',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不用 UPX：它会降低启动速度，且常被杀软误报
    console=CONSOLE,  # 默认 GUI（无控制台窗口）；诊断构建时置 CONSOLE=True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AccountBook',
)
