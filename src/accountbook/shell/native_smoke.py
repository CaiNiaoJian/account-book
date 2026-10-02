"""构建期原生窗口验证：使用显式隔离数据，加载真实前端并自动退出。"""

from __future__ import annotations

import json
import logging
import time

from ..api.state import create_context
from ..config import RuntimeSettings, build_config_store
from ..db.bootstrap import bootstrap_database
from ..db.session import Database
from ..logging_setup import setup_logging
from ..paths import get_paths
from .clr_runtime import resolve_clr_runtime
from .window import WebViewShell


def run_native_smoke() -> int:
    """检查真实 HWND、WebView2、React 渲染和带会话的 API；失败不打开浏览器。"""
    from ..app import BackendServer

    paths = get_paths()
    paths.ensure()
    settings = RuntimeSettings(dev=False, enable_tray=False)
    setup_logging(paths.log_file, settings)
    report: dict[str, object] = {"ok": False}
    report_path = paths.cache / "native-smoke.json"
    database = Database(paths.database)
    server = None

    class SmokeShell(WebViewShell):
        def _on_gui_ready(self) -> None:
            try:
                super()._on_gui_ready()
                if not self._window.events.loaded.wait(30):
                    raise RuntimeError("前端加载超时")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    page = self._window.evaluate_js(
                        "({boot: Boolean(window.__AB_BOOT__?.token),"
                        "rendered: Boolean(document.getElementById('root')?.childElementCount),"
                        "title: document.title})"
                    )
                    if page and page.get("boot") and page.get("rendered"):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("React 界面未渲染或会话注入失败")
                # 同一 WebView 中请求 API，验证页面 Cookie 生效。
                self._window.evaluate_js(
                    "window.__AB_SMOKE_STATUS__ = null;"
                    "fetch('/api/system/info').then(r => window.__AB_SMOKE_STATUS__ = r.status)"
                    ".catch(() => window.__AB_SMOKE_STATUS__ = -1); void 0"
                )
                deadline = time.monotonic() + 10
                status = None
                while status is None and time.monotonic() < deadline:
                    status = self._window.evaluate_js("window.__AB_SMOKE_STATUS__")
                    time.sleep(0.1)
                from webview.platforms import winforms

                hwnd = self._hwnd()
                report.update(hwnd=bool(hwnd), renderer=winforms.renderer, page=page, api_status=status)
                report["ok"] = bool(hwnd) and winforms.renderer == "edgechromium" and status == 200
            except Exception as exc:
                report["error"] = str(exc)
                logging.getLogger(__name__).exception("原生窗口冒烟测试失败")
            finally:
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                self.quit()

    try:
        bootstrap_database(database)
        ctx = create_context(paths=paths, settings=settings, config=build_config_store(paths))
        ctx.database = database
        server = BackendServer(ctx, settings)
        ctx.port = server.start()
        runtime = resolve_clr_runtime(paths)
        report["runtime"] = runtime
        if runtime is None:
            raise RuntimeError("没有可用的原生窗口运行时")
        shell = SmokeShell(ctx, settings, f"http://127.0.0.1:{ctx.port}/?token={ctx.token.value}", hidden=True)
        shell.start()
    except Exception as exc:
        report["error"] = str(exc)
        logging.getLogger(__name__).exception("原生窗口冒烟测试启动失败")
    finally:
        if server is not None:
            server.stop()
        database.dispose()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 4
