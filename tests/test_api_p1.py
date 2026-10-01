"""P1 业务接口用例 —— 把端到端验证固化成回归网。

与 ``.smoke-test/verify-api.ps1`` 的分工
----------------------------------------
那份 PowerShell 脚本验证"打包后的真实进程 + 真实 HTTP + 真实文件"，
用于发布前的人工验收；本文件验证"接口契约与业务规则"，
每次改动都能在几秒内跑完。两者都要有：
前者能发现打包/环境问题，后者能防止逻辑被改坏。

覆盖重点仍是**会悄悄算错数字**的地方：转账不计收支、分账金额守恒、
分类方向匹配、删除后余额回补。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("app_paths")

ClientFixture = tuple[TestClient, Any]


def _find(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in items:
        if item["name"] == name:
            return item
    raise AssertionError(f"未找到名为 {name} 的条目")


class TestMeta:
    def test_enums_come_from_domain(self, authed_client: ClientFixture) -> None:
        """枚举由后端统一下发，避免前端手抄一份取值列表。"""
        test_client, _ = authed_client
        body = test_client.get("/api/meta/enums").json()
        assert len(body["account_types"]) == 9
        assert body["transaction_types"] == ["expense", "income", "transfer", "adjust"]
        assert body["directions"] == ["in", "out"]

    def test_currencies_carry_minor_units(self, authed_client: ClientFixture) -> None:
        """日元没有小数位 —— 前端格式化金额依赖这个字段。"""
        test_client, _ = authed_client
        currencies = test_client.get("/api/meta/currencies").json()
        by_code = {item["code"]: item for item in currencies}
        assert len(currencies) == 12
        assert by_code["JPY"]["minor_units"] == 0
        assert by_code["CNY"]["minor_units"] == 2


class TestAccountsApi:
    def test_seed_accounts_and_overview(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        overview = test_client.get("/api/accounts/overview").json()
        assert overview["account_count"] == 3
        assert overview["net_worth_minor"] == 0
        assert [item["name"] for item in overview["accounts"]] == ["现金", "微信", "支付宝"]

    def test_create_duplicate_name_conflicts(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        payload = {"name": "招行储蓄卡", "type": "debit_card", "initial_balance_minor": 100000}
        assert test_client.post("/api/accounts", json=payload).status_code == 201
        duplicate = test_client.post("/api/accounts", json=payload)
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "conflict"

    def test_delete_blocked_while_used(self, authed_client: ClientFixture) -> None:
        """有流水时拒绝删除 —— 否则统计会出现找不到账户的空洞。"""
        test_client, _ = authed_client
        accounts = test_client.get("/api/accounts").json()
        cash = _find(accounts, "现金")
        created = test_client.post(
            "/api/transactions",
            json={"type": "expense", "account_id": cash["id"], "amount_minor": 100},
        )
        assert created.status_code == 201

        blocked = test_client.delete(f"/api/accounts/{cash['id']}")
        assert blocked.status_code == 409
        assert blocked.json()["details"]["transaction_count"] == 1


class TestCategoriesApi:
    def test_tree_is_deep_and_complete(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        tree = test_client.get("/api/categories/tree", params={"kind": "expense"}).json()
        assert len(tree) == 14
        food = _find(tree, "餐饮")
        assert len(food["children"]) == 8

        everything = test_client.get("/api/categories").json()
        assert len(everything) >= 100

    def test_create_child_validates_kind_and_path(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        tree = test_client.get("/api/categories/tree", params={"kind": "expense"}).json()
        food = _find(tree, "餐饮")

        created = test_client.post(
            "/api/categories",
            json={"name": "夜宵", "kind": "expense", "parent_id": food["id"]},
        )
        assert created.status_code == 201
        assert created.json()["path"].startswith(food["path"])

        # 同名兄弟
        assert (
            test_client.post(
                "/api/categories",
                json={"name": "夜宵", "kind": "expense", "parent_id": food["id"]},
            ).status_code
            == 409
        )
        # 方向不一致
        assert (
            test_client.post(
                "/api/categories",
                json={"name": "方向错误", "kind": "income", "parent_id": food["id"]},
            ).status_code
            == 422
        )

    def test_system_category_cannot_be_deleted(self, authed_client: ClientFixture) -> None:
        """内置分类可隐藏、可改名，但不可删除。"""
        test_client, _ = authed_client
        tree = test_client.get("/api/categories/tree", params={"kind": "expense"}).json()
        food = _find(tree, "餐饮")

        assert (
            test_client.post(f"/api/categories/{food['id']}/hide", params={"hidden": "true"}).status_code
            == 200
        )
        assert test_client.delete(f"/api/categories/{food['id']}").status_code == 409


class TestTransactionsApi:
    def _cash(self, test_client: TestClient) -> int:
        return _find(test_client.get("/api/accounts").json(), "现金")["id"]

    def _category(self, test_client: TestClient, kind: str, name: str) -> int:
        items = test_client.get("/api/categories", params={"kind": kind}).json()
        return _find(items, name)["id"]

    def test_record_expense_updates_balance(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": self._cash(test_client),
                "category_id": self._category(test_client, "expense", "午餐"),
                "amount_minor": 3500,
                "payee": "楼下面馆",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["direction"] == "out"
        assert body["category_name"] == "午餐"
        assert body["amount_minor"] == 3500

        overview = test_client.get("/api/accounts/overview").json()
        assert overview["accounts"][0]["balance_minor"] == -3500

    def test_float_amount_is_rejected(self, authed_client: ClientFixture) -> None:
        """金额必须是整数最小单位，浮点直接被挡在契约层。"""
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions",
            json={"type": "expense", "account_id": self._cash(test_client), "amount_minor": 12.34},
        )
        assert response.status_code == 422

    def test_income_category_rejected_for_expense(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": self._cash(test_client),
                "category_id": self._category(test_client, "income", "工资"),
                "amount_minor": 100,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_split_sum_must_match(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        lunch = self._category(test_client, "expense", "午餐")
        response = test_client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": self._cash(test_client),
                "amount_minor": 10000,
                "splits": [
                    {"category_id": lunch, "amount_minor": 6000},
                    {"category_id": lunch, "amount_minor": 3000},
                ],
            },
        )
        assert response.status_code == 422
        assert response.json()["details"]["difference_minor"] == 1000

    def test_transfer_needs_target(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions",
            json={"type": "transfer", "account_id": self._cash(test_client), "amount_minor": 100},
        )
        assert response.status_code == 422

    def test_unknown_field_is_rejected(self, authed_client: ClientFixture) -> None:
        """未知字段必须报错：静默忽略会让"改了却没生效"无从察觉。"""
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions",
            json={"type": "expense", "account_id": self._cash(test_client), "amount_minor": 100, "nope": 1},
        )
        assert response.status_code == 422

    def test_missing_account_is_404(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        response = test_client.post(
            "/api/transactions", json={"type": "expense", "account_id": 999999, "amount_minor": 100}
        )
        assert response.status_code == 404

    def test_transfer_excluded_from_income_expense(self, authed_client: ClientFixture) -> None:
        """转账不计收支 —— 否则"工资卡转支付宝"会让月支出凭空翻倍。"""
        test_client, _ = authed_client
        cash = self._cash(test_client)
        alipay = _find(test_client.get("/api/accounts").json(), "支付宝")["id"]

        test_client.post(
            "/api/transactions",
            json={"type": "expense", "account_id": cash, "amount_minor": 3500},
        )
        test_client.post(
            "/api/transactions",
            json={"type": "transfer", "account_id": cash, "to_account_id": alipay, "amount_minor": 20000},
        )

        overview = test_client.get("/api/accounts/overview").json()
        assert overview["net_worth_minor"] == -3500
        assert overview["assets_minor"] == 20000

        dashboard = test_client.get("/api/stats/dashboard").json()
        assert dashboard["month"]["expense_minor"] == 3500
        assert dashboard["month"]["transaction_count"] == 1

    def test_list_filter_search_and_pagination(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        cash = self._cash(test_client)
        lunch = self._category(test_client, "expense", "午餐")
        for index in range(3):
            test_client.post(
                "/api/transactions",
                json={
                    "type": "expense",
                    "account_id": cash,
                    "category_id": lunch,
                    "amount_minor": 1000 + index,
                    "payee": f"商户{index}" if index else "楼下面馆",
                },
            )

        listing = test_client.get("/api/transactions", params={"limit": 10}).json()
        assert listing["total"] == 3

        searched = test_client.get("/api/transactions", params={"keyword": "面馆"}).json()
        assert searched["total"] == 1

        by_category = test_client.get("/api/transactions", params={"category_ids": lunch}).json()
        assert by_category["total"] == 3

        # 逗号分隔与重复参数都应被接受（前者是更常见的书写习惯）
        comma = test_client.get("/api/transactions", params={"types": "expense,income"}).json()
        assert comma["total"] == 3
        repeated = test_client.get(
            "/api/transactions", params=[("types", "expense"), ("types", "income")]
        ).json()
        assert repeated["total"] == 3

        page = test_client.get("/api/transactions", params={"limit": 2, "offset": 0}).json()
        assert len(page["items"]) == 2
        assert page["total"] == 3, "总数必须是满足条件的总数，而不是当页条数"

    def test_delete_restores_balance_and_restore_brings_it_back(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        created = test_client.post(
            "/api/transactions",
            json={"type": "expense", "account_id": self._cash(test_client), "amount_minor": 3500},
        ).json()
        assert test_client.get("/api/accounts/overview").json()["accounts"][0]["balance_minor"] == -3500

        assert test_client.delete(f"/api/transactions/{created['id']}").status_code == 204
        assert test_client.get("/api/accounts/overview").json()["accounts"][0]["balance_minor"] == 0
        assert test_client.get("/api/stats/dashboard").json()["month"]["expense_minor"] == 0

        assert test_client.post(f"/api/transactions/{created['id']}/restore").status_code == 200
        assert test_client.get("/api/accounts/overview").json()["accounts"][0]["balance_minor"] == -3500

    def test_split_breakdown_prefers_splits(self, authed_client: ClientFixture) -> None:
        """分账记录的是钱真正的去向，汇总时必须优先于主分类。"""
        test_client, _ = authed_client
        lunch = self._category(test_client, "expense", "午餐")
        other = self._category(test_client, "expense", "其他支出")
        test_client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": self._cash(test_client),
                "category_id": other,
                "amount_minor": 10000,
                "splits": [
                    {"category_id": lunch, "amount_minor": 7000},
                    {"category_id": other, "amount_minor": 3000},
                ],
            },
        )

        today = date.today().isoformat()
        summary = test_client.get(
            "/api/stats/summary", params={"start": today, "end": today, "top_categories": 5}
        ).json()
        by_name = {item["category_name"]: item["amount_minor"] for item in summary["by_category"]}
        assert by_name["午餐"] == 7000
        assert by_name["其他支出"] == 3000


class TestStatsApi:
    def test_calendar_returns_every_day(self, authed_client: ClientFixture) -> None:
        """没有记账的日子也要占位，否则日历会缺格，看起来像应用坏了。"""
        test_client, _ = authed_client
        days = test_client.get(
            "/api/stats/calendar", params={"start": "2026-03-01", "end": "2026-03-07"}
        ).json()
        assert len(days) == 7
        assert all(day["transaction_count"] == 0 for day in days)
        assert all(day["has_entries"] is False for day in days)

    def test_dashboard_shape(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        body = test_client.get("/api/stats/dashboard").json()
        assert set(body) == {
            "reference_date",
            "net_worth",
            "month",
            "trend",
            "recent_transactions",
            "accounts",
        }
        assert len(body["trend"]) == 30
        assert body["month"]["income_change"] is None, "上期为 0 时环比无定义，不能编造百分比"

    def test_integrity_report_is_clean(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        report = test_client.get("/api/stats/integrity").json()
        assert report["ok"] is True
        assert report["stats"]["categories"] >= 100


class TestTaxonomyApi:
    def test_tag_lifecycle(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        assert test_client.post("/api/tags", json={"name": "出差"}).status_code == 201
        assert test_client.post("/api/tags", json={"name": "出差"}).status_code == 409
        assert test_client.get("/api/tags").json()[0]["name"] == "出差"

    def test_only_one_self_member(self, authed_client: ClientFixture) -> None:
        test_client, _ = authed_client
        test_client.post("/api/members", json={"name": "我", "is_self": True})
        test_client.post("/api/members", json={"name": "配偶", "is_self": True})
        members = test_client.get("/api/members").json()
        assert sum(1 for item in members if item["is_self"]) == 1


class TestJsonCharset:
    def test_json_responses_declare_utf8(self, authed_client: ClientFixture) -> None:
        """必须显式声明 charset。

        否则 Windows PowerShell 5.1 等客户端会按 Latin-1 解码，
        所有中文变成乱码 —— 表现为"按分类名查找全部落空"，
        看起来像接口返回了错数据。
        """
        test_client, _ = authed_client
        response = test_client.get("/api/meta/currencies")
        assert "charset=utf-8" in response.headers["content-type"].lower()
