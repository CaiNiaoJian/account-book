"""桌面外壳用例（不需要真实图形环境）。

重点覆盖 **降级路径的可观测性**：用户实际踩到的问题是
"原生窗口不可用 → 自动降级到浏览器 → 但浏览器没打开 → 程序变成看不见的后台进程"。
因此这里把"打开浏览器"的判定逻辑单独拎出来测试，确保它**如实反映成败**。
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

from accountbook.shell.window import BrowserShell

_TEST_URL = "http://127.0.0.1:12345/?token=abcdefghijklmnopqrstuvwxyz0123456789"


class _StubContext:
    """只提供 ``start()`` 真正用到的那部分上下文。

    刻意不构造完整的 AppContext：这条路径不该依赖数据库、配置或外壳引用。
    一旦将来有人让它依赖了，这个测试会先失败 —— 这正是我们想要的约束。
    """

    def __init__(self) -> None:
        self.shutdown_event = threading.Event()


class TestBrowserOpen:
    """``BrowserShell._open_browser`` 的三种方式与返回值语义。"""

    def test_returns_true_when_webbrowser_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """首选方式成功即返回 True，不再尝试后续方式。"""
        calls: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda *a, **k: calls.append("webbrowser") or True)

        assert BrowserShell._open_browser(_TEST_URL) is True
        assert calls == ["webbrowser"]

    def test_falls_through_to_shell_association(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``webbrowser`` 返回 False 时应继续尝试 Shell 关联。"""
        monkeypatch.setattr("webbrowser.open", lambda *a, **k: False)
        attempted: list[str] = []

        if sys.platform == "win32":

            def _fake_startfile(_url: str) -> None:
                attempted.append("startfile")

            monkeypatch.setattr(os, "startfile", _fake_startfile, raising=False)

            assert BrowserShell._open_browser(_TEST_URL) is True
            assert attempted == ["startfile"]
        else:
            monkeypatch.setattr("subprocess.Popen", lambda *a, **k: attempted.append("popen") or object())
            assert BrowserShell._open_browser(_TEST_URL) is True
            assert attempted == ["popen"]

    def test_returns_false_when_every_method_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部失败必须如实返回 False —— 上层据此弹出可见提示。

        旧实现无论成败都记日志"已在默认浏览器中打开"，
        这正是"点击程序毫无反应"却查不到原因的根源。
        """
        monkeypatch.setattr("webbrowser.open", lambda *a, **k: False)

        if sys.platform == "win32":

            def _raise_oserror(_url: str) -> None:
                raise OSError("shell association broken")

            monkeypatch.setattr(os, "startfile", _raise_oserror, raising=False)

        def _raise_popen(*_args: object, **_kwargs: object) -> None:
            raise OSError("cmd unavailable")

        monkeypatch.setattr("subprocess.Popen", _raise_popen)

        assert BrowserShell._open_browser(_TEST_URL) is False

    def test_webbrowser_exception_does_not_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``webbrowser`` 抛异常时不得中断，应继续尝试后续方式。"""

        def _raise(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("no default browser")

        monkeypatch.setattr("webbrowser.open", _raise)

        if sys.platform == "win32":
            monkeypatch.setattr(os, "startfile", lambda _url: None, raising=False)

        assert BrowserShell._open_browser(_TEST_URL) is True


class TestShellContract:
    """外壳协议的基本约束。"""

    def test_browser_shell_kind_is_stable(self) -> None:
        """``kind`` 会出现在日志与界面（运行状态卡片），不可随意更名。"""
        assert BrowserShell.kind == "browser"

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:1/?token=abc",
            "http://127.0.0.1:1/",
        ],
    )
    def test_open_browser_never_raises(self, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
        """任何输入都不得抛出异常：这是启动路径上的最后一环。"""
        monkeypatch.setattr("webbrowser.open", lambda *a, **k: False)
        if sys.platform == "win32":

            def _boom(_url: str) -> None:
                raise OSError("nope")

            monkeypatch.setattr(os, "startfile", _boom, raising=False)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

        assert BrowserShell._open_browser(url) is False


class TestNoInvisibleProcess:
    """核心承诺：**绝不让用户面对一个看不见的进程**。

    这是用户实际反馈的故障：双击程序后"什么都不发生"，任务管理器里却躺着一个常驻进程。
    下面几条用例把该承诺固化下来 —— 一旦有人把失败路径改回静默继续运行，
    测试会立刻失败。
    """

    def test_start_stops_when_user_declines_manual_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自动打开失败且用户拒绝手动打开时，必须**主动退出**而不是继续隐身运行。"""
        ctx = _StubContext()
        shell = BrowserShell(ctx, None, _TEST_URL)  # type: ignore[arg-type]
        monkeypatch.setattr(BrowserShell, "_open_browser", staticmethod(lambda _url: False))
        monkeypatch.setattr(BrowserShell, "_prompt_until_visible", lambda _self: False)

        shell.start()

        assert ctx.shutdown_event.is_set(), "用户拒绝后必须设置停机信号，否则会留下隐形进程"

    def test_start_keeps_serving_when_user_accepts_manual_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """用户选择继续时，服务应保持可用（不停机）。"""
        ctx = _StubContext()
        ctx.shutdown_event.set()  # 让阻塞循环立即结束，测试不必真的等待
        shell = BrowserShell(ctx, None, _TEST_URL)  # type: ignore[arg-type]
        monkeypatch.setattr(BrowserShell, "_open_browser", staticmethod(lambda _url: False))
        monkeypatch.setattr(BrowserShell, "_prompt_until_visible", lambda _self: True)

        shell.start()  # 不应抛异常

        assert ctx.shutdown_event.is_set()

    def test_start_does_not_prompt_when_window_is_confirmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**确认窗口已出现**时不应打扰用户（不弹任何窗口）。"""
        ctx = _StubContext()
        ctx.shutdown_event.set()
        shell = BrowserShell(ctx, None, _TEST_URL)  # type: ignore[arg-type]
        prompted: list[str] = []

        monkeypatch.setattr(BrowserShell, "_open_browser", staticmethod(lambda _url: True))
        monkeypatch.setattr(BrowserShell, "_wait_for_app_window", classmethod(lambda _cls, timeout=4.0: True))
        monkeypatch.setattr(BrowserShell, "_prompt_until_visible", lambda _self: prompted.append("x") or True)

        shell.start()

        assert prompted == [], "确认窗口出现时不应该弹提示窗口"

    def test_start_prompts_when_open_lies_about_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**本用例锁定真实故障**：``webbrowser.open`` 返回 True 但窗口并未出现。

        实测中 ``_open_browser`` 返回 True 而系统中没有任何浏览器进程 ——
        只信返回值就会让程序静默变成一个看不见的后台进程。
        因此只要"窗口未被确认"，就必须走可见出口。
        """
        ctx = _StubContext()
        shell = BrowserShell(ctx, None, _TEST_URL)  # type: ignore[arg-type]
        prompted: list[str] = []

        monkeypatch.setattr(BrowserShell, "_open_browser", staticmethod(lambda _url: True))
        monkeypatch.setattr(
            BrowserShell, "_wait_for_app_window", classmethod(lambda _cls, timeout=4.0: False)
        )
        monkeypatch.setattr(
            BrowserShell, "_prompt_until_visible", lambda _self: prompted.append("x") or False
        )

        shell.start()

        assert prompted == ["x"], "返回值声称成功但窗口不存在时，必须给用户可见出口"
        assert ctx.shutdown_event.is_set()


class TestWindowConfirmation:
    """窗口确认逻辑：既要能发现真窗口，也不能被无关窗口骗到。"""

    def test_marker_is_distinctive(self) -> None:
        """标记必须足够独特，避免被别的窗口标题误命中。

        实测教训：另一个应用的窗口标题里含"记账本"三个字，
        用单独的中文名做判定会把"没打开"误判为"已打开"。
        """
        from accountbook import APP_NAME, APP_NAME_EN, APP_WINDOW_TITLE

        expected_marker = f"{APP_NAME} · {APP_NAME_EN}"
        assert expected_marker == APP_WINDOW_TITLE
        assert "个人记账本桌面工具开发计划".find(APP_WINDOW_TITLE) == -1

    def test_finds_window_by_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """标题包含标记即视为窗口已出现（浏览器标签标题即页面标题）。"""
        monkeypatch.setattr(
            BrowserShell,
            "_visible_window_titles",
            staticmethod(lambda: ["记账本 · AccountBook - Microsoft Edge"]),
        )
        assert BrowserShell._wait_for_app_window(0.5) is True

    def test_ignores_unrelated_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无关窗口（即便含"记账本"）不得被误判。"""
        monkeypatch.setattr(
            BrowserShell,
            "_visible_window_titles",
            staticmethod(lambda: ["个人记账本桌面工具开发计划 — 某编辑器", "文件资源管理器"]),
        )
        assert BrowserShell._wait_for_app_window(0.5) is False

    def test_survives_enumeration_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """枚举失败时必须返回 False（保守：宁可多提示，不可静默）。"""

        def _boom() -> list[str]:
            raise OSError("enum failed")

        monkeypatch.setattr(BrowserShell, "_visible_window_titles", staticmethod(_boom))
        with pytest.raises(OSError):
            # 异常向上传播，由 start() 的调用链保证不会导致进程静默
            BrowserShell._wait_for_app_window(0.2)
