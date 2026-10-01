"""P6 的 API 层用例。

服务层已经逐一验证过算法，这里只验证**接线**：
字段映射、查询参数、错误码、以及几个只在路由层才成立的约定
（`fill` 之后强弹要一并了结、强弹的出口要返回 409/422 而不是 500）。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

PERIOD = "2026-10"


def _client(authed_client: tuple[TestClient, Any]) -> TestClient:
    return authed_client[0]


def _account_id(authed_client: tuple[TestClient, Any]) -> int:
    return _client(authed_client).get("/api/accounts").json()[0]["id"]


def _source(authed_client) -> dict:
    return (
        _client(authed_client)
        .post(
            "/api/payroll/sources",
            json={"name": "主职", "kind": "salary", "account_id": _account_id(authed_client)},
        )
        .json()
    )


def _with_components(authed_client) -> dict:
    client = _client(authed_client)
    source = _source(authed_client)
    client.post(
        "/api/payroll/components",
        json={
            "name": "基本工资",
            "kind": "basic",
            "source_id": source["id"],
            "amount_minor": 2_000_000,
            "sort_order": 1,
        },
    )
    client.post(
        "/api/payroll/components",
        json={
            "name": "个税",
            "kind": "tax",
            "sign": -1,
            "source_id": source["id"],
            "amount_minor": 150_000,
            "sort_order": 9,
        },
    )
    return source


class TestPayrollApi:
    def test_zero_data(self, authed_client) -> None:
        client = _client(authed_client)
        assert client.get("/api/payroll/sources").json()["items"] == []
        assert client.get("/api/payroll/components").json()["items"] == []
        assert client.get("/api/payroll/records").json()["items"] == []
        assert client.get("/api/payroll/upcoming").json()["items"] == []
        # 零数据时不该编一个 0% 的同比
        body = client.get(f"/api/payroll/overview?period={PERIOD}").json()
        assert body["delta"]["gross"] is None
        assert body["months"]

    def test_source_crud_and_restore(self, authed_client) -> None:
        client = _client(authed_client)
        source = _source(authed_client)
        assert source["name"] == "主职"

        renamed = client.patch(f"/api/payroll/sources/{source['id']}", json={"name": "改个名"}).json()
        assert renamed["name"] == "改个名"
        # 没传的字段不该被清掉
        assert renamed["kind"] == "salary"

        assert client.delete(f"/api/payroll/sources/{source['id']}").status_code == 204
        assert client.get("/api/payroll/sources").json()["items"] == []
        assert client.post(f"/api/payroll/sources/{source['id']}/restore").status_code == 200

    def test_source_validation(self, authed_client) -> None:
        response = _client(authed_client).post("/api/payroll/sources", json={"name": ""})
        assert response.status_code == 422

    def test_payday_rule_and_pay_date_confidence(self, authed_client) -> None:
        client = _client(authed_client)
        source = _source(authed_client)
        # 没配规则时是 inferred
        body = client.get(f"/api/payroll/pay-date?source_id={source['id']}&period={PERIOD}").json()
        assert body["confidence"] == "inferred"

        client.put(
            f"/api/payroll/sources/{source['id']}/rule",
            json={"day_of_month": 15, "weekend_policy": "advance"},
        )
        body = client.get(f"/api/payroll/pay-date?source_id={source['id']}&period={PERIOD}").json()
        # 2026 年的节假日没录入 → 只能是 assumed
        assert body["confidence"] == "assumed"

    def test_rule_validation(self, authed_client) -> None:
        source = _source(authed_client)
        response = _client(authed_client).put(
            f"/api/payroll/sources/{source['id']}/rule", json={"day_of_month": 32}
        )
        assert response.status_code == 422

    def test_compute_does_not_persist(self, authed_client) -> None:
        """改组成项时界面要能立刻看到新结果，而"先执行再看结果"不可接受。"""
        client = _client(authed_client)
        source = _with_components(authed_client)
        body = client.get(f"/api/payroll/compute?source_id={source['id']}").json()
        assert body["gross_minor"] == 2_000_000
        assert body["net_minor"] == 1_850_000
        # 试算不该产生记录
        assert client.get("/api/payroll/records").json()["items"] == []

    def test_record_lifecycle(self, authed_client) -> None:
        client = _client(authed_client)
        source = _with_components(authed_client)
        record = client.post(
            "/api/payroll/records",
            json={"source_id": source["id"], "period": PERIOD, "pay_date": "2026-10-15"},
        ).json()
        assert record["status"] == "draft"
        assert record["gross_minor"] == 2_000_000

        # 同期重复建档被拒
        duplicate = client.post("/api/payroll/records", json={"source_id": source["id"], "period": PERIOD})
        assert duplicate.status_code == 409

        filled = client.post(
            f"/api/payroll/records/{record['id']}/fill", json={"create_transaction": True}
        ).json()
        assert filled["created"] is True
        assert filled["transaction_id"]

        # 幂等
        again = client.post(
            f"/api/payroll/records/{record['id']}/fill", json={"create_transaction": True}
        ).json()
        assert again["created"] is False

        # 已入账不可重算、不可跳过、不可删
        assert client.post(f"/api/payroll/records/{record['id']}/recompute").status_code == 409
        assert (
            client.post(f"/api/payroll/records/{record['id']}/skip", json={"reason": "反悔"}).status_code
            == 409
        )
        assert client.delete(f"/api/payroll/records/{record['id']}").status_code == 409

    def test_skip_requires_reason(self, authed_client) -> None:
        client = _client(authed_client)
        source = _with_components(authed_client)
        record = client.post(
            "/api/payroll/records", json={"source_id": source["id"], "period": PERIOD}
        ).json()
        # 空原因被参数校验拦下
        assert (
            client.post(f"/api/payroll/records/{record['id']}/skip", json={"reason": ""}).status_code == 422
        )
        body = client.post(
            f"/api/payroll/records/{record['id']}/skip", json={"reason": "这个月没有工资"}
        ).json()
        assert body["status"] == "skipped"
        assert body["skip_reason"] == "这个月没有工资"

    def test_overview_with_records(self, authed_client) -> None:
        client = _client(authed_client)
        source = _with_components(authed_client)
        record = client.post(
            "/api/payroll/records",
            json={"source_id": source["id"], "period": PERIOD, "pay_date": "2026-10-15"},
        ).json()
        client.post(f"/api/payroll/records/{record['id']}/fill", json={"create_transaction": False})
        body = client.get(f"/api/payroll/overview?period={PERIOD}&history_months=6").json()
        assert body["current"]["gross_minor"] == 2_000_000
        assert body["current"]["count"] == 1
        assert len(body["months"]) == 6

    def test_invalid_period_is_400(self, authed_client) -> None:
        response = _client(authed_client).get("/api/payroll/overview?period=2026")
        assert response.status_code in {400, 422}


class TestInsuranceApi:
    def test_zero_data(self, authed_client) -> None:
        client = _client(authed_client)
        assert client.get("/api/insurance/items").json()["items"] == []
        assert client.get("/api/insurance/profiles").json()["items"] == []
        assert client.get("/api/insurance/accounts").json()["total_minor"] == 0
        body = client.get("/api/insurance/overview?start_period=2026-01&end_period=2026-12").json()
        assert body["by_kind"] == []
        # 没数据时不该编一个 0% 的单位占比
        assert body["employer_share"] is None

    def test_ensure_items_seeds_names_only(self, authed_client) -> None:
        """**字典只铺名称，比例一律留 0。**"""
        client = _client(authed_client)
        body = client.post("/api/insurance/items/ensure").json()
        assert body["count"] == 9
        assert all(item["personal_rate_bps"] == 0 for item in body["items"])
        assert all(item["rates_filled"] is False for item in body["items"])

        # 幂等且不覆盖已填比例
        client.put("/api/insurance/items", json={"kind": "pension", "personal_rate_bps": 800})
        again = client.post("/api/insurance/items/ensure").json()
        pension = next(item for item in again["items"] if item["kind"] == "pension")
        assert pension["personal_rate_bps"] == 800

    def test_item_validation(self, authed_client) -> None:
        response = _client(authed_client).put(
            "/api/insurance/items", json={"kind": "pension", "personal_rate_bps": 20000}
        )
        assert response.status_code == 422

    def test_profile_and_compute_reports_incomplete(self, authed_client) -> None:
        client = _client(authed_client)
        client.post("/api/insurance/items/ensure")
        profile = client.post(
            "/api/insurance/profiles",
            json={"name": "本人", "social_base_minor": 2_000_000},
        ).json()
        body = client.get(f"/api/insurance/compute?profile_id={profile['id']}").json()
        # **比例没填齐时合计必然是 0**，界面要能显示成"待配置"而不是"缴得少"
        assert body["personal_total_minor"] == 0
        assert body["incomplete"] is True
        assert body["unfilled_items"]

    def test_contribution_and_balances(self, authed_client) -> None:
        client = _client(authed_client)
        client.post("/api/insurance/items/ensure")
        for item in client.get("/api/insurance/items").json()["items"]:
            if item["kind"] != "housing_fund":
                client.put("/api/insurance/items", json={"kind": item["kind"], "enabled": False})
        client.put(
            "/api/insurance/items",
            json={
                "kind": "housing_fund",
                "personal_rate_bps": 1200,
                "employer_rate_bps": 1200,
            },
        )
        profile = client.post(
            "/api/insurance/profiles",
            json={"name": "本人", "social_base_minor": 1_000_000, "housing_base_minor": 1_000_000},
        ).json()

        first = client.post(
            "/api/insurance/contributions",
            json={"profile_id": profile["id"], "period": PERIOD},
        ).json()
        assert first["count"] == 1
        # 幂等：重复写入不会让余额翻倍
        client.post(
            "/api/insurance/contributions",
            json={"profile_id": profile["id"], "period": PERIOD},
        )
        balances = client.get(f"/api/insurance/accounts?profile_id={profile['id']}").json()
        assert balances["balances"]["housing_fund"] == 240_000

    def test_clamp_is_reported(self, authed_client) -> None:
        """收敛必须报出来：用户填 3 万、系统按 2.4 万算，不说明他会以为算错了。"""
        client = _client(authed_client)
        client.put(
            "/api/insurance/items",
            json={"kind": "pension", "personal_rate_bps": 800, "cap_base_minor": 2_400_000},
        )
        profile = client.post(
            "/api/insurance/profiles",
            json={"name": "本人", "social_base_minor": 3_000_000},
        ).json()
        body = client.get(f"/api/insurance/compute?profile_id={profile['id']}").json()
        pension = next(item for item in body["items"] if item["kind"] == "pension")
        assert pension["raw_base_minor"] == 3_000_000
        assert pension["base_minor"] == 2_400_000
        assert pension["clamped"] == "cap"

    def test_withdrawal_over_balance_is_409(self, authed_client) -> None:
        client = _client(authed_client)
        client.put(
            "/api/insurance/items",
            json={"kind": "housing_fund", "personal_rate_bps": 1200, "employer_rate_bps": 1200},
        )
        profile = client.post(
            "/api/insurance/profiles",
            json={"name": "本人", "housing_base_minor": 1_000_000},
        ).json()
        client.post(
            "/api/insurance/contributions",
            json={"profile_id": profile["id"], "period": PERIOD},
        )
        item_id = next(
            item["id"]
            for item in client.get("/api/insurance/items").json()["items"]
            if item["kind"] == "housing_fund"
        )
        response = client.post(
            "/api/insurance/withdrawals",
            json={
                "profile_id": profile["id"],
                "item_id": item_id,
                "amount_minor": 99_999_999,
                "occurred_at": "2026-10-20",
                "reason": "rent",
            },
        )
        assert response.status_code == 409

    def test_statement_reports_difference(self, authed_client) -> None:
        client = _client(authed_client)
        client.put("/api/insurance/items", json={"kind": "pension", "personal_rate_bps": 800})
        profile = client.post(
            "/api/insurance/profiles",
            json={"name": "本人", "social_base_minor": 1_000_000},
        ).json()
        client.post(
            "/api/insurance/contributions",
            json={"profile_id": profile["id"], "period": "2026-01"},
        )
        body = client.get(f"/api/insurance/statement?profile_id={profile['id']}&year=2026").json()
        assert body["computed_personal_minor"] == 80_000
        assert body["difference"]["personal_minor"] is None

        updated = client.put(
            "/api/insurance/statement",
            json={
                "profile_id": profile["id"],
                "year": 2026,
                "expected_personal_minor": 90_000,
                "interest_minor": 5_000,
            },
        ).json()
        assert updated["difference"]["personal_minor"] == -10_000
        assert updated["interest_minor"] == 5_000


class TestWorkdayApi:
    def test_zero_data(self, authed_client) -> None:
        body = _client(authed_client).get("/api/workdays").json()
        assert body["items"] == []
        assert body["covered_years"] == []

    def test_override_and_delete(self, authed_client) -> None:
        client = _client(authed_client)
        client.put(
            "/api/workdays",
            json={"day": "2026-01-01", "is_workday": False, "name": "元旦"},
        )
        body = client.get("/api/workdays?year=2026").json()
        assert body["count"] == 1
        assert body["covered_years"] == [2026]
        assert body["items"][0]["kind"] == "holiday"
        assert client.delete("/api/workdays/2026-01-01").status_code == 204
        assert client.get("/api/workdays").json()["items"] == []

    def test_import_reports_unparsed_lines(self, authed_client) -> None:
        """**无法解析的行原样返回** —— 否则那个假期会悄悄变成工作日。"""
        client = _client(authed_client)
        body = client.post(
            "/api/workdays/import",
            json={"text": "休 2026-01-01~2026-01-03 元旦\n班 2026-01-04\n这行看不懂"},
        ).json()
        assert body["saved"] == 4
        assert body["unparsed"] == ["这行看不懂"]


class TestSchedulerApi:
    def test_zero_data(self, authed_client) -> None:
        client = _client(authed_client)
        assert client.get("/api/scheduler/tasks").json()["items"] == []
        assert client.get("/api/scheduler/tasks/due").json()["items"] == []
        assert client.get("/api/scheduler/history").json()["items"] == []
        health = client.get("/api/scheduler/health").json()
        assert health["total"] == 0
        assert health["failure_ratio"] is None
        assert client.get("/api/scheduler/blocking").json()["count"] == 0
        assert client.get("/api/notifications").json()["items"] == []
        assert client.get("/api/notifications/unread-count").json()["unread"] == 0

    def test_task_upsert_and_run(self, authed_client) -> None:
        client = _client(authed_client)
        created = client.post(
            "/api/scheduler/tasks",
            json={
                "code": "custom-1",
                "name": "提醒交房租",
                "kind": "custom",
                "rule": {"frequency": "daily", "at": "09:00"},
            },
        ).json()
        assert created["next_run_at"]
        assert created["implemented"] is True

        # 同 code 再来一次是更新而不是新建
        again = client.post(
            "/api/scheduler/tasks",
            json={
                "code": "custom-1",
                "name": "改名了",
                "kind": "custom",
                "rule": {"frequency": "daily", "at": "10:00"},
            },
        ).json()
        assert again["id"] == created["id"]
        assert again["name"] == "改名了"

        # 未到期 → dry_run 什么也不做
        assert client.post("/api/scheduler/run?dry_run=true").json()["count"] == 0

    def test_not_implemented_task_reports_skipped(self, authed_client) -> None:
        """**诚实性保证**：一个"成功但什么也没做"的备份任务
        会让用户以为备份在跑。"""
        client = _client(authed_client)
        created = client.post(
            "/api/scheduler/tasks",
            json={
                "code": "backup-1",
                "name": "每日备份",
                "kind": "backup",
                "rule": {"frequency": "once", "date": "2020-01-01", "at": "03:00"},
            },
        ).json()
        assert created["implemented"] is False
        assert "尚未实现" in created["not_implemented_reason"]
        assert created["next_run_at"] is None  # 一次性任务已过

    def test_task_validation(self, authed_client) -> None:
        client = _client(authed_client)
        assert client.post(
            "/api/scheduler/tasks",
            json={"code": "x", "name": "x", "rule": {"frequency": "hourly"}},
        ).status_code in {400, 422}
        assert client.post(
            "/api/scheduler/tasks",
            json={"code": "x", "name": "x", "rule": {"frequency": "daily", "at": "99:99"}},
        ).status_code in {400, 422}


class TestPromptApi:
    def _prompt(self, client: TestClient, **kwargs) -> dict:

        # 直接经服务层造提示（路由层没有"造提示"的入口，那是调度器的职责）
        return kwargs

    def test_blocking_gate_has_exits(self, authed_client) -> None:
        """**强弹必须有出口。** 只有"必须填"会让真的没工资的月份变成死锁。

        这里走**真实路径**：建发薪任务 → 立即执行 → 产生强弹 →
        稍后提醒（有上限）→ 跳过（必须给原因）。
        """
        client = _client(authed_client)
        source = _with_components(authed_client)
        client.put(f"/api/payroll/sources/{source['id']}/rule", json={"day_of_month": 15})
        task = client.post(
            "/api/scheduler/tasks",
            json={
                "code": "payday-1",
                "name": "发薪日",
                "kind": "payday",
                "rule": {"frequency": "monthly", "day_of_month": 15, "at": "09:00"},
                "ref_id": source["id"],
            },
        ).json()
        assert client.post(f"/api/scheduler/tasks/{task['id']}/run").json()["status"] == "success"

        blocking = client.get("/api/scheduler/blocking").json()
        assert blocking["count"] == 1
        prompt_id = blocking["items"][0]["id"]
        assert blocking["items"][0]["snooze_left"] == 3

        # 稍后提醒三次可以，第四次到顶 → 409 且给出下一步
        for _ in range(3):
            assert (
                client.post(f"/api/scheduler/prompts/{prompt_id}/snooze", json={"minutes": 30}).status_code
                == 200
            )
        capped = client.post(f"/api/scheduler/prompts/{prompt_id}/snooze", json={"minutes": 30})
        assert capped.status_code == 409
        assert "跳过" in capped.text

        # 跳过必须给原因
        assert client.post(f"/api/scheduler/prompts/{prompt_id}/skip", json={"reason": ""}).status_code == 422
        skipped = client.post(
            f"/api/scheduler/prompts/{prompt_id}/skip", json={"reason": "这个月没有工资"}
        ).json()
        assert skipped["status"] == "skipped"
        # 处理完之后门禁放开
        assert client.get("/api/scheduler/blocking").json()["count"] == 0


class TestNotificationApi:
    def test_flow(self, authed_client) -> None:
        client = _client(authed_client)
        task = client.post(
            "/api/scheduler/tasks",
            json={
                "code": "rent-1",
                "name": "提醒交房租",
                "kind": "custom",
                "note": "今天要交房租",
                "rule": {"frequency": "daily", "at": "09:00"},
            },
        ).json()
        assert client.post(f"/api/scheduler/tasks/{task['id']}/run").json()["status"] == "success"

        body = client.get("/api/notifications").json()
        assert body["count"] == 1
        assert body["unread"] == 1
        assert client.get("/api/notifications/unread-count").json()["unread"] == 1

        first = body["items"][0]["id"]
        assert client.post(f"/api/notifications/{first}/read").json()["unread"] == 0
        assert client.get("/api/notifications?unread_only=true").json()["count"] == 0


class TestAuth:
    def test_requires_auth(self, client) -> None:
        """薪酬与五险一金是隐私，未授权必须 401。"""
        test_client, _ = client
        for path in (
            "/api/payroll/sources",
            "/api/payroll/records",
            "/api/insurance/profiles",
            "/api/insurance/accounts",
            "/api/scheduler/tasks",
            "/api/scheduler/blocking",
            "/api/notifications",
            "/api/workdays",
        ):
            assert test_client.get(path).status_code == 401, path
