"""进程收尾用例 —— 固化"进程一定会结束"这一承诺。

真实故障：日志打出"已退出"，但任务管理器里进程仍在运行。
原因是托盘（pystray）线程是非守护线程，其窗口创建失败时无法被停止，
解释器会一直等它。用户看到的就是"关不掉的隐形进程"。

这些用例通过**子进程**验证真实行为，而不是只测函数返回值 ——
线程存活与否是进程级事实，只有真跑一个进程才能确认。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: 仓库根目录（tests/ 的上一级）
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_python(code: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """在子进程中执行一段代码（源码运行方式，走 src 布局）。"""
    script = f"import sys; sys.path.insert(0, {str(_REPO_ROOT / 'src')!r})\n" + textwrap.dedent(code)
    # 命令完全由测试自身构造（sys.executable + 固定参数）
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=str(_REPO_ROOT),
    )


class TestFinalizeExit:
    """``accountbook.__main__._finalize_exit`` 的行为。"""

    def test_returns_code_when_no_threads_linger(self) -> None:
        """没有残留线程时应原样返回退出码。"""
        result = _run_python(
            """
            from accountbook.__main__ import _finalize_exit
            print("RESULT", _finalize_exit(0))
            """
        )
        assert result.returncode == 0, result.stderr
        assert "RESULT 0" in result.stdout

    def test_force_exits_when_non_daemon_thread_lingers(self) -> None:
        """存在残留非守护线程时，进程必须**立即结束**而不是挂住。

        这里模拟托盘线程的形态：非守护 + 永久阻塞。
        若收尾逻辑失效，子进程会一直等到超时 —— 那正是用户遇到的现象。
        """
        result = _run_python(
            """
            import threading, time
            from accountbook.__main__ import _finalize_exit

            def _never_ends():
                while True:
                    time.sleep(1)

            threading.Thread(target=_never_ends, name="fake-tray", daemon=False).start()
            time.sleep(0.3)
            print("BEFORE", flush=True)
            print("RESULT", _finalize_exit(0), flush=True)
            print("SHOULD NOT REACH HERE", flush=True)
            """
        )
        # 关键断言：进程在超时前就结束了（os._exit 生效）
        assert result.returncode == 0, f"进程未能自我结束。stderr={result.stderr}"
        assert "BEFORE" in result.stdout
        assert "SHOULD NOT REACH HERE" not in result.stdout, "强制退出未生效"

    def test_daemon_threads_do_not_block_exit(self) -> None:
        """守护线程不应触发强制退出（正常情况不该走到那条路径）。"""
        result = _run_python(
            """
            import threading, time
            from accountbook.__main__ import _finalize_exit

            def _never_ends():
                while True:
                    time.sleep(1)

            threading.Thread(target=_never_ends, name="fake-http", daemon=True).start()
            time.sleep(0.2)
            print("RESULT", _finalize_exit(7))
            """
        )
        assert result.returncode == 0, result.stderr
        assert "RESULT 7" in result.stdout

    @pytest.mark.parametrize("code", [0, 1, 2, 3])
    def test_exit_code_is_preserved(self, code: int) -> None:
        """强制退出不得改变业务退出码（否则脚本无法判断成败）。"""
        result = _run_python(
            f"""
            import threading, time
            from accountbook.__main__ import _finalize_exit

            threading.Thread(target=lambda: time.sleep(30), name="stuck", daemon=False).start()
            time.sleep(0.2)
            _finalize_exit({code})
            """
        )
        assert result.returncode == code
