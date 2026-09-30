#!/usr/bin/env python
"""开发启动器 —— 免安装运行本应用。

用法::

    python run.py                 # 正常启动
    python run.py --dev           # 配合 `cd web && npm run dev` 做前端热更新
    python run.py --doctor        # 环境自检
    python run.py --print-paths   # 打印路径与环境信息

为什么需要这个文件而不是直接 ``python -m accountbook``：
    本项目采用 ``src/`` 布局，未安装时 Python 找不到 ``accountbook`` 包。
    与其要求每位开发者先 ``pip install -e .``（并因此多一层构建后端依赖），
    不如用一个 12 行的启动器把 ``src`` 注入 ``sys.path``。
    PyInstaller 打包时采用同样的 ``pathex`` 注入策略，两条路径保持一致。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent
_SRC = _REPO_ROOT / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from accountbook.__main__ import main  # noqa: E402 —— 必须在 sys.path 注入之后导入

if __name__ == "__main__":
    raise SystemExit(main())
