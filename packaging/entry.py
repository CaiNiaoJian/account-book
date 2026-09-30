"""PyInstaller 打包入口。

为什么需要这样一个 5 行的文件，而不是直接打包 ``src/accountbook/__main__.py``：

1. **包内相对导入**：``__main__.py`` 使用 ``from . import ...`` 这类相对导入，
   它必须作为 ``accountbook`` 包的一部分被导入，而不是当作顶层脚本执行。
2. **源码运行与打包运行共用一条路径**：本文件把 ``src`` 注入 ``sys.path``，
   与仓库根目录的 ``run.py`` 完全一致 —— 两种运行方式不会出现行为差异。
3. **frozen 环境**：打包后 ``src`` 并不存在，注入语句无效但无害；
   依赖由 ``.spec`` 的 ``pathex`` 与 ``hiddenimports`` 保证。
"""

from __future__ import annotations

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from accountbook.__main__ import main  # noqa: E402 —— 必须在上面的路径注入之后导入

if __name__ == "__main__":
    raise SystemExit(main())
