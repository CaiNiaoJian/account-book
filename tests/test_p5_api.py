"""P5 的 API 层用例（台账 / 报表 / 导出）。

分两层验证：服务层测"算得对不对"，这里测"路由把请求正确翻译成了服务调用"
（字段映射、查询参数解析、导出格式与响应头）。
两者会一起失败的地方很多，但失败的**原因**完全不同。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

TODAY = "2026-10-01"
WINDOW = f"start=2026-09-11&end={TODAY}"


def _client(authed_client: tuple[TestClient, Any]) -> TestClient:
    return authed_client[0]


def _account_id(authed_client: tuple[TestClient, Any]) -> int:
    return _client(authed_client).get("/api/accounts").json()[0]["id"]


class TestLedgerApi:
    def test_zero_data_ledger(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        body = client.get(f"/api/ledger/{account_id}?{WINDOW}").json()
        assert body["entries"] == []
        assert body["count"] == 0
        assert body["check"]["balanced"] is True
        assert body["opening_balance_minor"] == body["closing_balance_minor"]

    def test_entries_carry_running_balance(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        for amount in (30_000, 20_000):
            client.post(
                "/api/transactions",
                json={
                    "type": "expense",
                    "account_id": account_id,
                    "amount_minor": amount,
                    "occurred_at": f"{TODAY}T12:00:00",
                },
            )
        body = client.get(f"/api/ledger/{account_id}?{WINDOW}").json()
        assert body["count"] == 2
        running = body["opening_balance_minor"]
        for entry in body["entries"]:
            running += entry["signed_minor"]
            assert entry["running_balance_minor"] == running
        assert body["outflow_minor"] == 50_000
        assert body["check"]["balanced"] is True

    def test_requires_start_and_end(self, authed_client) -> None:
        """台账没有区间就没有"期初"这个概念，必须显式给出。"""
        response = _client(authed_client).get(f"/api/ledger/{_account_id(authed_client)}")
        assert response.status_code == 422

    def test_reversed_range_is_400(self, authed_client) -> None:
        response = _client(authed_client).get(
            f"/api/ledger/{_account_id(authed_client)}?start=2026-10-01&end=2026-09-01"
        )
        assert response.status_code in {400, 422}

    def test_unknown_account_is_404(self, authed_client) -> None:
        response = _client(authed_client).get(f"/api/ledger/999999?{WINDOW}")
        assert response.status_code == 404

    def test_trial_balance(self, authed_client) -> None:
        body = _client(authed_client).get("/api/ledger/trial-balance").json()
        assert body["balanced"] is True
        assert body["broken_transfer_ids"] == []

    def test_reconcile_creates_adjustment(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        client.post(
            "/api/transactions",
            json={
                "type": "income",
                "account_id": account_id,
                "amount_minor": 10_000,
                "occurred_at": f"{TODAY}T09:00:00",
            },
        )
        body = client.post(
            f"/api/ledger/{account_id}/reconcile",
            json={"actual_balance_minor": 10_500, "as_of": TODAY},
        ).json()
        assert body["difference_minor"] == 500
        assert body["created_transaction_id"] is not None
        assert body["new_balance_minor"] == 10_500
        # 对账之后试算平衡仍必须成立（调整计入余额但不计入收支）
        assert client.get("/api/ledger/trial-balance").json()["balanced"] is True

    def test_reconcile_preview_only(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        body = client.post(
            f"/api/ledger/{account_id}/reconcile",
            json={
                "actual_balance_minor": 5_000,
                "as_of": TODAY,
                "create_adjustment": False,
            },
        ).json()
        assert body["difference_minor"] == 5_000
        assert body["created_transaction_id"] is None


class TestReportApi:
    def test_zero_data_report(self, authed_client) -> None:
        body = _client(authed_client).get("/api/reports?kind=monthly").json()
        assert body["schema"] == "accountbook.report/1"
        assert body["kind"] == "monthly"
        assert body["sections"]
        assert body["cover"]["headline"]

    def test_with_data(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": account_id,
                "amount_minor": 12_345,
                "occurred_at": f"{TODAY}T12:00:00",
            },
        )
        body = client.get(f"/api/reports?kind=daily&start={TODAY}&end={TODAY}&kind=custom").json()
        # 路由对 kind=custom 要求显式区间
        assert body["period"]["start"] == TODAY

    def test_section_keys_endpoint(self, authed_client) -> None:
        body = _client(authed_client).get("/api/reports/section-keys").json()
        assert "overview" in body["items"]
        assert "payroll" in body["items"]

    def test_include_filters_sections(self, authed_client) -> None:
        body = _client(authed_client).get("/api/reports?kind=monthly&include=overview,breakdown").json()
        assert [item["key"] for item in body["sections"]] == ["overview", "breakdown"]

    def test_unknown_include_is_400(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports?kind=monthly&include=nope")
        assert response.status_code in {400, 422}

    def test_custom_requires_range(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports?kind=custom")
        assert response.status_code in {400, 422}

    def test_unknown_kind_is_400(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports?kind=fortnightly")
        assert response.status_code in {400, 422}


class TestExportApi:
    def test_markdown_export_headers(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=md")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "charset=utf-8" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        assert ".md" in response.headers["content-disposition"]
        # 报告依赖"今天"，不该被任何一层缓存住
        assert response.headers["cache-control"] == "no-store"
        # 带 BOM：Windows 记事本按本地代码页打开无 BOM 的 UTF-8 会乱码
        assert response.content.startswith(b"\xef\xbb\xbf")

    def test_html_export_is_self_contained(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=html")
        text = response.content.decode("utf-8")
        assert text.startswith("<!doctype html>")
        assert "<script" not in text
        assert "<link" not in text

    def test_pdf_export(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")

    def test_inline_omits_attachment(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=html&inline=true")
        assert response.headers["content-disposition"].startswith("inline")

    def test_unknown_format_is_400(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=docx")
        assert response.status_code in {400, 422}

    def test_export_respects_include(self, authed_client) -> None:
        response = _client(authed_client).get("/api/reports/export?kind=monthly&format=md&include=overview")
        text = response.content.decode("utf-8-sig")
        assert "## 收支概览" in text
        assert "## 结构分解" not in text

    def test_requires_auth(self, client) -> None:
        """台账与报表里有全部财务明细，未授权必须 401。"""
        test_client, _ = client
        assert test_client.get("/api/ledger/trial-balance").status_code == 401
        assert test_client.get("/api/reports").status_code == 401
        assert test_client.get("/api/reports/export").status_code == 401
