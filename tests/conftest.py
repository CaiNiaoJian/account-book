"""pytest 公共夹具。

设计原则
--------
* **测试绝不触碰真实用户数据目录**：所有用例通过 ``ACCOUNTBOOK_DATA_DIR``
  指向 pytest 的 ``tmp_path``，并在用例结束后清理路径缓存。
* **不依赖图形环境**：P0 的用例只覆盖路径解析、安全原语与 HTTP 契约，
  需要真实窗口的用例一律打 ``@pytest.mark.gui``，在受限环境中自动跳过。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from accountbook.api.server import create_app
from accountbook.api.state import create_context
from accountbook.config import RuntimeSettings, build_config_store
from accountbook.db.bootstrap import bootstrap_database
from accountbook.db.session import Database
from accountbook.paths import AppPaths, get_paths, reset_paths_cache

#: 测试中使用的固定环回端口（仅用于构造 Origin / base_url，不实际监听）
TEST_PORT = 45999


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把数据目录隔离到临时路径，并清空路径缓存。

    ``autouse=True``：任何用例都不应该因为忘记隔离而写到用户的真实账本目录。
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ACCOUNTBOOK_DATA_DIR", str(data_dir))
    # 确保便携模式判定不受仓库中是否存在 portable.flag 影响
    monkeypatch.delenv("ACCOUNTBOOK_PORTABLE", raising=False)
    monkeypatch.delenv("ACCOUNTBOOK_HOME", raising=False)
    reset_paths_cache()
    yield data_dir
    reset_paths_cache()


@pytest.fixture
def app_paths(_isolated_data_dir: Path, tmp_path: Path) -> AppPaths:
    """已创建目录的路径集合，并把只读资源根指向临时目录。

    为什么要替换 ``resources``：前端构建产物位于仓库内，
    测试若直接依赖它会变成"必须先构建前端才能跑单元测试"，
    这是不必要的耦合。这里用一份最小 index.html 代替。
    """
    paths = get_paths()
    paths.ensure()

    resources = tmp_path / "resources"
    web_dist = resources / "web_dist"
    web_dist.mkdir(parents=True)
    (web_dist / "index.html").write_text(
        '<!doctype html><html><head><title>test</title></head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (web_dist / "assets").mkdir()
    (web_dist / "assets" / "app.js").write_text("console.log('test')", encoding="utf-8")
    (resources / "icons").mkdir()

    return dataclasses.replace(paths, resources=resources)


@pytest.fixture
def settings() -> RuntimeSettings:
    """测试用运行时设置（单实例与托盘关闭，避免干扰用例）。"""
    return RuntimeSettings(single_instance=False, enable_tray=False, log_to_console=False)


@pytest.fixture
def client(app_paths: AppPaths, settings: RuntimeSettings) -> Iterator[tuple[TestClient, Any]]:
    """带上下文的测试客户端。

    产出 ``(client, ctx)``：多数用例需要 ``ctx.token`` 来构造合法请求。
    ``base_url`` 必须是环回地址，否则会被 Host 守卫拒绝——这正是我们要验证的行为之一。

    这里**顺带把数据库准备好**（迁移 + 内置数据），与生产启动流程保持一致：
    如果测试里手工建表，就永远测不到"迁移链能否在新库上跑通"这件事 ——
    而那恰恰是最需要在每次改动后验证的部分。
    """
    config = build_config_store(app_paths)
    ctx = create_context(paths=app_paths, settings=settings, config=config)
    ctx.port = TEST_PORT

    database = Database(app_paths.database)
    bootstrap_database(database)
    ctx.database = database

    app = create_app(ctx)
    try:
        with TestClient(app, base_url=f"http://127.0.0.1:{TEST_PORT}") as test_client:
            yield test_client, ctx
    finally:
        database.dispose()


@pytest.fixture
def authed_client(client: tuple[TestClient, Any]) -> tuple[TestClient, Any]:
    """带合法令牌 Cookie 的客户端。"""
    test_client, ctx = client
    test_client.cookies.set("ab_session", ctx.token.value)
    return test_client, ctx


def read_config(paths: AppPaths) -> dict[str, Any]:
    """读取持久化的偏好文件（供断言使用）。"""
    return json.loads(paths.config_file.read_text(encoding="utf-8"))
