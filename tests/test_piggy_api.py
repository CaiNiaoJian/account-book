"""P4 的 API 层用例（存钱罐 / 储蓄目标）。

与 `test_piggy.py` 的服务层用例分开：那边验证规则本身，
这边验证**路由把请求正确翻译成了服务调用**（字段映射、默认值、
`exclude_unset` 的语义、错误码）。两者会同时失败的地方很多，
但失败的**原因**完全不同，混在一起会让人分不清是规则错了还是接线错了。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

TODAY = "2026-10-01"


def _client(authed_client: tuple[TestClient, Any]) -> TestClient:
    return authed_client[0]


def _account_id(authed_client: tuple[TestClient, Any]) -> int:
    return _client(authed_client).get("/api/accounts").json()[0]["id"]


class TestPiggyBankApi:
    def test_create_and_read_back(self, authed_client) -> None:
        client = _client(authed_client)
        created = client.post(
            "/api/piggy/banks",
            json={
                "name": "相机基金",
                "target_amount_minor": 500_000,
                "target_name": "一台相机",
                "initial_minor": 100_000,
            },
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        assert payload["balance_minor"] == 100_000
        assert payload["ratio"] == 0.2
        assert payload["milestones"] == []

        detail = client.get(f"/api/piggy/banks/{payload['id']}").json()
        # 建罐时放的初始金额要出现在存入记录里，否则用户看不到它怎么来的
        assert len(detail["deposits"]) == 1
        assert detail["deposits"][0]["amount_minor"] == 100_000

    def test_zero_data_list_is_empty(self, authed_client) -> None:
        """零数据：列表为空、合计为 0，而不是报错。"""
        body = _client(authed_client).get("/api/piggy/banks").json()
        assert body["items"] == []
        assert body["count"] == 0
        assert body["saved_minor"] == 0
        assert body["active_target_minor"] == 0

    def test_unknown_field_is_rejected(self, authed_client) -> None:
        """`extra="forbid"`：拼错字段名要报错，而不是静默忽略。"""
        response = _client(authed_client).post(
            "/api/piggy/banks",
            json={"name": "x", "target_amount_minor": 1000, "taget_name": "typo"},
        )
        assert response.status_code == 422

    def test_patch_distinguishes_absent_from_null(self, authed_client) -> None:
        """只传 `name` 时，其它字段必须原样保留。

        这是 `exclude_unset` 的意义：如果路由用 `model_dump()`，
        没传的字段会以 `None` 覆盖掉已有值 —— 改个名字顺手把截止日期清掉。
        """
        client = _client(authed_client)
        bank = client.post(
            "/api/piggy/banks",
            json={
                "name": "旧名",
                "target_amount_minor": 100_000,
                "deadline": TODAY,
                "priority": 8,
            },
        ).json()

        updated = client.patch(f"/api/piggy/banks/{bank['id']}", json={"name": "新名"}).json()
        assert updated["name"] == "新名"
        assert updated["deadline"] == TODAY, "没传 deadline 不该被清掉"
        assert updated["priority"] == 8

        # 显式传 null 才清空
        cleared = client.patch(f"/api/piggy/banks/{bank['id']}", json={"deadline": None}).json()
        assert cleared["deadline"] is None
        assert cleared["name"] == "新名"

    def test_delete_and_restore(self, authed_client) -> None:
        client = _client(authed_client)
        bank = client.post("/api/piggy/banks", json={"name": "x", "target_amount_minor": 1000}).json()
        assert client.delete(f"/api/piggy/banks/{bank['id']}").status_code == 204
        assert client.get("/api/piggy/banks").json()["items"] == []
        assert len(client.get("/api/piggy/banks?include_deleted=true").json()["items"]) == 1
        assert client.post(f"/api/piggy/banks/{bank['id']}/restore").status_code == 200

    def test_bad_target_is_422(self, authed_client) -> None:
        response = _client(authed_client).post(
            "/api/piggy/banks", json={"name": "x", "target_amount_minor": 0}
        )
        assert response.status_code == 422


class TestPiggyDepositApi:
    def _bank(self, authed_client) -> dict[str, Any]:
        return (
            _client(authed_client)
            .post("/api/piggy/banks", json={"name": "罐", "target_amount_minor": 100_000})
            .json()
        )

    def test_deposit_and_withdraw(self, authed_client) -> None:
        client = _client(authed_client)
        bank = self._bank(authed_client)
        client.post(f"/api/piggy/banks/{bank['id']}/deposits", json={"amount_minor": 30_000})
        body = client.get(f"/api/piggy/banks/{bank['id']}/deposits").json()
        assert body["balance_minor"] == 30_000
        assert body["count"] == 1

    def test_overdraw_returns_409(self, authed_client) -> None:
        client = _client(authed_client)
        bank = self._bank(authed_client)
        client.post(f"/api/piggy/banks/{bank['id']}/deposits", json={"amount_minor": 5_000})
        response = client.post(f"/api/piggy/banks/{bank['id']}/deposits", json={"amount_minor": -5_001})
        assert response.status_code == 409, response.text
        # 冲突响应要带上余额，界面才能提示"你只有 50"
        assert response.json()["details"]["balance_minor"] == 5_000

    def test_zero_amount_is_422(self, authed_client) -> None:
        bank = self._bank(authed_client)
        response = _client(authed_client).post(
            f"/api/piggy/banks/{bank['id']}/deposits", json={"amount_minor": 0}
        )
        assert response.status_code == 422

    def test_delete_deposit(self, authed_client) -> None:
        client = _client(authed_client)
        bank = self._bank(authed_client)
        deposit = client.post(f"/api/piggy/banks/{bank['id']}/deposits", json={"amount_minor": 1_000}).json()
        assert client.delete(f"/api/piggy/deposits/{deposit['id']}").status_code == 204
        assert client.get(f"/api/piggy/banks/{bank['id']}/deposits").json()["count"] == 0


class TestPiggyRuleApi:
    def _bank(self, authed_client) -> dict[str, Any]:
        return (
            _client(authed_client)
            .post("/api/piggy/banks", json={"name": "罐", "target_amount_minor": 100_000})
            .json()
        )

    def test_upsert_rule_then_preview_and_run(self, authed_client) -> None:
        client = _client(authed_client)
        bank = self._bank(authed_client)
        updated = client.put(
            f"/api/piggy/banks/{bank['id']}/rule",
            json={"strategy": "daily_fixed", "fixed_amount_minor": 500},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["rule"]["strategy"] == "daily_fixed"

        due = client.get(f"/api/piggy/rules/due?today={TODAY}").json()
        assert len(due["items"]) == 1

        # 默认 dry_run=True：对自动扣钱的功能来说"先执行再看结果"不可接受
        preview = client.post("/api/piggy/rules/run", json={"today": TODAY}).json()
        assert preview["dry_run"] is True
        assert preview["total_minor"] == 500
        assert client.get(f"/api/piggy/banks/{bank['id']}").json()["balance_minor"] == 0

        real = client.post("/api/piggy/rules/run", json={"today": TODAY, "dry_run": False}).json()
        assert real["dry_run"] is False
        assert client.get(f"/api/piggy/banks/{bank['id']}").json()["balance_minor"] == 500

    def test_apply_roundup_to_transaction(self, authed_client) -> None:
        client = _client(authed_client)
        bank = self._bank(authed_client)
        client.put(f"/api/piggy/banks/{bank['id']}/rule", json={"strategy": "roundup"})
        account_id = _account_id(authed_client)
        transaction = client.post(
            "/api/transactions",
            json={
                "type": "expense",
                "account_id": account_id,
                "amount_minor": 3_281,
                "occurred_at": f"{TODAY}T12:00:00",
            },
        ).json()

        applied = client.post("/api/piggy/rules/apply", json=[transaction["id"]]).json()
        assert applied["count"] == 1
        assert applied["total_minor"] == 19
        # 幂等：再调一次不重复
        again = client.post("/api/piggy/rules/apply", json=[transaction["id"]]).json()
        assert again["count"] == 0

    def test_empty_transaction_ids_is_422(self, authed_client) -> None:
        response = _client(authed_client).post("/api/piggy/rules/apply", json=[])
        assert response.status_code == 422

    def test_deduct_without_account_is_422(self, authed_client) -> None:
        bank = self._bank(authed_client)
        response = _client(authed_client).put(
            f"/api/piggy/banks/{bank['id']}/rule",
            json={"strategy": "roundup", "deduct_from_account": True},
        )
        assert response.status_code in {400, 422}, response.text


class TestPiggyAchieveApi:
    def test_achieve_creates_expense(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        bank = client.post(
            "/api/piggy/banks",
            json={
                "name": "相机基金",
                "target_amount_minor": 100_000,
                "target_name": "相机",
                "initial_minor": 120_000,
            },
        ).json()
        assert bank["status"] == "achieved"

        result = client.post(f"/api/piggy/banks/{bank['id']}/achieve", json={"account_id": account_id})
        assert result.status_code == 200, result.text
        assert result.json()["settled_minor"] == 120_000
        assert result.json()["balance_minor"] == 0

    def test_achieve_without_account_is_422(self, authed_client) -> None:
        client = _client(authed_client)
        bank = client.post(
            "/api/piggy/banks",
            json={"name": "x", "target_amount_minor": 1000, "initial_minor": 1000},
        ).json()
        response = client.post(f"/api/piggy/banks/{bank['id']}/achieve", json={})
        assert response.status_code in {400, 422}, response.text


class TestGoalApi:
    def test_zero_data(self, authed_client) -> None:
        body = _client(authed_client).get("/api/goals").json()
        assert body["items"] == []
        assert body["saved_minor"] == 0

    def test_create_and_contribute(self, authed_client) -> None:
        client = _client(authed_client)
        goal = client.post("/api/goals", json={"name": "旅行", "target_amount_minor": 100_000}).json()
        assert goal["saved_minor"] == 0

        body = client.post(f"/api/goals/{goal['id']}/contributions", json={"amount_minor": 40_000}).json()
        assert body["saved_minor"] == 40_000
        assert body["items"][0]["amount_minor"] == 40_000

        detail = client.get(f"/api/goals/{goal['id']}").json()
        assert len(detail["contributions"]) == 1

    def test_progress_from_account_balance(self, authed_client) -> None:
        client = _client(authed_client)
        account_id = _account_id(authed_client)
        goal = client.post(
            "/api/goals",
            json={
                "name": "首付",
                "target_amount_minor": 500_000,
                "account_id": account_id,
            },
        ).json()
        client.post(
            "/api/transactions",
            json={
                "type": "income",
                "account_id": account_id,
                "amount_minor": 200_000,
                "occurred_at": f"{TODAY}T09:00:00",
            },
        )
        detail = client.get(f"/api/goals/{goal['id']}").json()
        assert detail["saved_minor"] == 200_000
        assert detail["ratio"] == 0.4

    def test_delete_and_restore(self, authed_client) -> None:
        client = _client(authed_client)
        goal = client.post("/api/goals", json={"name": "x", "target_amount_minor": 1000}).json()
        assert client.delete(f"/api/goals/{goal['id']}").status_code == 204
        assert client.get("/api/goals").json()["items"] == []
        assert client.post(f"/api/goals/{goal['id']}/restore").status_code == 200

    def test_delete_contribution(self, authed_client) -> None:
        client = _client(authed_client)
        goal = client.post("/api/goals", json={"name": "x", "target_amount_minor": 100_000}).json()
        client.post(f"/api/goals/{goal['id']}/contributions", json={"amount_minor": 5_000})
        rows = client.get(f"/api/goals/{goal['id']}/contributions").json()["items"]
        assert client.delete(f"/api/goals/contributions/{rows[0]['id']}").status_code == 204
        assert client.get(f"/api/goals/{goal['id']}").json()["saved_minor"] == 0

    def test_overdraw_returns_409(self, authed_client) -> None:
        client = _client(authed_client)
        goal = client.post("/api/goals", json={"name": "x", "target_amount_minor": 100_000}).json()
        client.post(f"/api/goals/{goal['id']}/contributions", json={"amount_minor": 1_000})
        response = client.post(f"/api/goals/{goal['id']}/contributions", json={"amount_minor": -1_001})
        assert response.status_code == 409

    def test_requires_auth(self, client) -> None:
        """没有会话 Cookie 时必须 401 —— 存钱罐里有什么同样是隐私。"""
        test_client, _ = client
        assert test_client.get("/api/piggy/banks").status_code == 401
        assert test_client.get("/api/goals").status_code == 401
