"""HTTP 契约与访问守卫用例。

覆盖三件事：
    1. **守卫真的生效**：无令牌拒绝、非法 Host 拒绝、非法 Origin 拒绝；
    2. **合法请求真的可用**：偏好读写、系统信息、前端托管与启动注入；
    3. **路径穿越被挡住**：静态资源不能读到 web_dist 之外的文件。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PORT, read_config

pytestmark = pytest.mark.usefixtures("app_paths")


# -----------------------------------------------------------------------------
# 探活
# -----------------------------------------------------------------------------
class TestHealth:
    def test_health_is_public(self, client: tuple[TestClient, Any]) -> None:
        """``/health`` 不需要令牌 —— 启动自检与外部探活依赖它。"""
        test_client, _ = client
        response = test_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["app"] == "AccountBook"

    def test_health_leaks_no_environment_details(self, client: tuple[TestClient, Any]) -> None:
        """公开端点绝不能携带路径等环境信息。"""
        test_client, _ = client
        body = test_client.get("/health").json()
        assert set(body) == {"status", "app", "version", "phase"}


# -----------------------------------------------------------------------------
# 访问守卫
# -----------------------------------------------------------------------------
class TestAccessGuard:
    def test_restart_entry_replaces_stale_cookie(self, client: tuple[TestClient, Any]) -> None:
        test_client, ctx = client
        test_client.cookies.set("ab_session", "previous-process-token")
        response = test_client.get(f"/?token={ctx.token.value}")
        assert response.status_code == 200
        assert response.cookies.get("ab_session") == ctx.token.value
        assert test_client.get("/api/system/info").status_code == 200

    def test_invalid_entry_does_not_reuse_valid_cookie(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        response = test_client.get("/?token=wrong-process-token")
        assert response.status_code == 401
        assert "ab_session" not in response.cookies

    def test_explicit_bearer_keeps_priority(self, client: tuple[TestClient, Any]) -> None:
        test_client, ctx = client
        response = test_client.get(
            f"/?token={ctx.token.value}", headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_api_without_token_is_rejected(self, client: tuple[TestClient, Any]) -> None:
        test_client, _ = client
        response = test_client.get("/api/system/info")
        assert response.status_code == 401

    def test_api_with_valid_bearer_token(self, client: tuple[TestClient, Any]) -> None:
        test_client, ctx = client
        response = test_client.get(
            "/api/system/info",
            headers={"Authorization": f"Bearer {ctx.token.value}"},
        )
        assert response.status_code == 200

    def test_api_with_valid_cookie(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        assert test_client.get("/api/system/info").status_code == 200

    def test_api_with_wrong_token_is_rejected(self, client: tuple[TestClient, Any]) -> None:
        test_client, _ = client
        response = test_client.get(
            "/api/system/info",
            headers={"Authorization": "Bearer definitely-not-the-token"},
        )
        assert response.status_code == 401

    def test_foreign_host_is_rejected(self, app_paths: Any, settings: Any) -> None:
        """Host 非环回 → 403（DNS rebinding 防护）。"""
        from accountbook.api.server import create_app
        from accountbook.api.state import create_context
        from accountbook.config import build_config_store

        ctx = create_context(paths=app_paths, settings=settings, config=build_config_store(app_paths))
        ctx.port = TEST_PORT
        with TestClient(create_app(ctx), base_url="http://evil.example.com") as test_client:
            response = test_client.get("/health")
        assert response.status_code == 403

    def test_foreign_origin_on_api_is_rejected(self, client: tuple[TestClient, Any]) -> None:
        """携带外部 Origin 的写操作 → 403（CSRF 防护）。"""
        test_client, ctx = client
        response = test_client.patch(
            "/api/system/preferences",
            json={"theme": "dark"},
            headers={
                "Authorization": f"Bearer {ctx.token.value}",
                "Origin": "https://evil.example.com",
            },
        )
        assert response.status_code == 403

    def test_own_origin_is_accepted(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        response = test_client.get(
            "/api/system/info",
            headers={"Origin": f"http://127.0.0.1:{TEST_PORT}"},
        )
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# 系统信息
# -----------------------------------------------------------------------------
class TestSystemInfo:
    def test_info_shape_and_mode(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        body = test_client.get("/api/system/info").json()
        assert body["phase"] == "P6"
        assert body["port"] == TEST_PORT
        assert body["paths"]["data_dir"]
        # 环境信息只用于本机展示，不应包含除路径外的任何用户数据字段
        assert "transactions" not in json.dumps(body)

    def test_info_exposes_data_dir_provenance(self, authed_client: tuple[TestClient, Any]) -> None:
        """接口必须说明数据目录的来源与尝试记录。

        这是"应用悄悄换了数据存放位置"这类问题唯一的排查入口：
        用户与支持人员都从这一处拿到事实。
        """
        test_client, _ = authed_client
        paths = test_client.get("/api/system/info").json()["paths"]
        assert paths["data_dir_source"]
        assert isinstance(paths["data_dir_attempts"], list)
        assert "shell" in test_client.get("/api/system/info").json()

    def test_reveal_data_dir_needs_token(self, client: tuple[TestClient, Any]) -> None:
        test_client, _ = client
        assert test_client.post("/api/system/reveal-data-dir").status_code == 401


# -----------------------------------------------------------------------------
# 偏好读写
# -----------------------------------------------------------------------------
class TestPreferences:
    def test_defaults_are_sane(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        prefs = test_client.get("/api/system/preferences").json()["preferences"]
        assert prefs["theme"] == "system"
        assert prefs["language"] == "zh-CN"
        assert prefs["money_color_scheme"] == "cn"

    def test_patch_persists_to_disk(self, authed_client: tuple[TestClient, Any], app_paths: Any) -> None:
        """偏好变更必须落盘 —— 否则"记住上次主题"这个需求就是假的。"""
        test_client, _ = authed_client
        response = test_client.patch("/api/system/preferences", json={"theme": "dark", "language": "en-US"})
        assert response.status_code == 200
        assert response.json()["preferences"]["theme"] == "dark"

        assert app_paths.config_file.exists()
        stored = read_config(app_paths)
        assert stored["theme"] == "dark"
        assert stored["language"] == "en-US"

    def test_patch_notifies_subscribers(self, authed_client: tuple[TestClient, Any], app_paths: Any) -> None:
        """主题变更需通知订阅者（桌面外壳据此同步原生标题栏）。"""
        test_client, ctx = authed_client
        received: list[str] = []
        ctx.config.subscribe(lambda prefs: received.append(prefs.theme))

        test_client.patch("/api/system/preferences", json={"theme": "light"})
        assert received == ["light"]

    def test_invalid_enum_is_rejected(self, authed_client: tuple[TestClient, Any]) -> None:
        """非法枚举值由 Pydantic 拦下（422），不进入业务层。"""
        test_client, _ = authed_client
        response = test_client.patch("/api/system/preferences", json={"theme": "neon"})
        assert response.status_code == 422

    def test_empty_patch_is_noop(self, authed_client: tuple[TestClient, Any]) -> None:
        test_client, _ = authed_client
        response = test_client.patch("/api/system/preferences", json={})
        assert response.status_code == 200
        assert response.json()["preferences"]["theme"] == "system"

    def test_unknown_preference_keys_are_preserved(self, app_paths: Any) -> None:
        """向前兼容：新版写入的未知字段不能被旧版擦除（REQ-10 预留扩展）。

        注意：必须在**构造 ConfigStore 之前**写入文件，
        否则测的是内存状态而不是"读旧文件后再写回"的真实路径。
        """
        from accountbook.config import ConfigStore

        app_paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        app_paths.config_file.write_text(
            json.dumps({"schema_version": 1, "theme": "dark", "future_feature_flag": True}),
            encoding="utf-8",
        )
        store = ConfigStore(app_paths.config_file)
        assert store.snapshot().model_dump().get("future_feature_flag") is True

        store.update(language="en-US")
        stored = read_config(app_paths)
        assert stored["future_feature_flag"] is True
        assert stored["language"] == "en-US"
        assert stored["theme"] == "dark"


# -----------------------------------------------------------------------------
# 前端托管
# -----------------------------------------------------------------------------
class TestFrontendHosting:
    @pytest.mark.parametrize("suffix", ["js", "css", "png", "svg"])
    def test_missing_asset_does_not_return_session_html(
        self, client: tuple[TestClient, Any], suffix: str,
    ) -> None:
        test_client, ctx = client
        response = test_client.get(f"/assets/missing.{suffix}")
        assert response.status_code == 404
        assert ctx.token.value not in response.text
        assert "__AB_BOOT__" not in response.text

    def test_index_requires_token(self, client: tuple[TestClient, Any]) -> None:
        """页面入口含注入的令牌，因此必须受保护。"""
        test_client, _ = client
        assert test_client.get("/").status_code == 401

    def test_index_injects_boot_payload(self, client: tuple[TestClient, Any]) -> None:
        test_client, ctx = client
        response = test_client.get(f"/?token={ctx.token.value}")
        assert response.status_code == 200
        assert "window.__AB_BOOT__" in response.text
        assert ctx.token.value in response.text
        assert response.headers["cache-control"] == "no-store"

    def test_index_sets_session_cookie(self, client: tuple[TestClient, Any]) -> None:
        """首次带 token 的导航必须种下 Cookie，之后前端无需再管令牌。"""
        test_client, ctx = client
        response = test_client.get(f"/?token={ctx.token.value}")
        assert response.cookies.get("ab_session") == ctx.token.value

    def test_security_headers_are_present(self, client: tuple[TestClient, Any]) -> None:
        """安全响应头必须随每个响应下发（REQ-14）。

        其中 ``connect-src 'self'`` 是"不上云"的**技术保证**：
        即使未来某处引入第三方脚本，页面也无法把账目数据发往外部地址。
        """
        test_client, ctx = client
        response = test_client.get(f"/?token={ctx.token.value}")
        csp = response.headers.get("content-security-policy", "")
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers.get("x-frame-options") == "DENY"
        assert response.headers.get("referrer-policy") == "no-referrer"
        assert "camera=()" in response.headers.get("permissions-policy", "")

    def test_assets_are_served_without_token(self, client: tuple[TestClient, Any]) -> None:
        """静态资源允许匿名获取，避免"Cookie 未生效 → 白屏"。"""
        test_client, _ = client
        response = test_client.get("/assets/app.js")
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    def test_spa_deep_link_returns_index(self, client: tuple[TestClient, Any]) -> None:
        test_client, ctx = client
        response = test_client.get(f"/transactions?token={ctx.token.value}")
        assert response.status_code == 200
        assert "window.__AB_BOOT__" in response.text

    @pytest.mark.parametrize(
        "attack",
        [
            "/../pyproject.toml",
            "/..%2fpyproject.toml",
            "/assets/../../pyproject.toml",
        ],
    )
    def test_path_traversal_is_blocked(self, client: tuple[TestClient, Any], attack: str) -> None:
        """路径穿越必须失败：既不能读到文件，也不能泄露内容。"""
        test_client, ctx = client
        response = test_client.get(f"{attack}?token={ctx.token.value}")
        assert "build-system" not in response.text
        assert response.status_code in {200, 400, 404}


# -----------------------------------------------------------------------------
# 缺失前端产物的兜底
# -----------------------------------------------------------------------------
class TestMissingFrontend:
    def test_fallback_page_when_not_built(self, app_paths: Any, settings: Any, tmp_path: Any) -> None:
        """未构建前端时给开发者一页明确指引，而不是白屏或 500。"""
        import dataclasses

        from accountbook.api.server import create_app
        from accountbook.api.state import create_context
        from accountbook.config import build_config_store

        bare = dataclasses.replace(app_paths, resources=tmp_path / "no-web-dist")
        ctx = create_context(paths=bare, settings=settings, config=build_config_store(bare))
        ctx.port = TEST_PORT
        with TestClient(create_app(ctx), base_url=f"http://127.0.0.1:{TEST_PORT}") as test_client:
            response = test_client.get(f"/?token={ctx.token.value}")
        assert response.status_code == 200
        assert "前端尚未构建" in response.text
