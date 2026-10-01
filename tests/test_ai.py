"""AI 分析（P5）的用例。

重点在三件事，它们都是"出问题时用户会被误导"的地方：

1. **离线回落必须永远给出可用的内容**，并如实标注来源 ——
   让用户以为内容是模型说的、实际是几条 if-else，那才是真的欺骗；
2. **商户名与备注从来不进 payload** —— 不是"默认脱敏所以小心"，
   而是根本不构造这些字段；
3. **脱敏的边界要可测**：去绝对金额、换掉用户自取的名字，
   但**保留分类名**（否则分析什么也说不出来）。

联网路径用 `httpx.MockTransport` 验证：**不联网、不需要真 key**
也能检查请求体与 SSE 解析。否则这条路径只能靠"人工试一次"，
而那种验证在 CI 里等于没有。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from accountbook.core.domain import TransactionType
from accountbook.core.errors import ValidationError
from accountbook.db.migrations import run_migrations
from accountbook.db.seed import ensure_seed_data
from accountbook.db.session import Database
from accountbook.services import accounts as accounts_service
from accountbook.services import ai as ai_service
from accountbook.services import budgets as budgets_service
from accountbook.services import categories as categories_service
from accountbook.services import piggy as piggy_service
from accountbook.services import reports
from accountbook.services import transactions as transactions_service

TODAY = date.today()
WINDOW_START = TODAY - timedelta(days=20)

#: 一个足够独特的商户名，用来断言它**没有**出现在 payload 里
SECRET_PAYEE = "张某某诊所-发票号998877"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ledger.db")
    run_migrations(database)
    with database.session() as session:
        ensure_seed_data(session)
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database):
    with db.session() as active:
        yield active


def _account(session, index: int = 0):
    return accounts_service.list_accounts(session)[index]


def _category(session, name: str, kind: str = "expense") -> int:
    for item in categories_service.list_categories(session, kind=kind):
        if item.name == name:
            return item.id
    return categories_service.create_category(session, name=name, kind=kind).id


def _seed_report(session, *, with_secret_payee: bool = True):
    account = _account(session)
    for offset, amount in enumerate((180_000, 120_000, 60_000)):
        transactions_service.create_transaction(
            session,
            type=TransactionType.EXPENSE.value,
            account_id=account.id,
            amount_minor=amount,
            occurred_at=datetime.combine(TODAY - timedelta(days=offset), datetime.min.time()).replace(
                hour=12
            ),
            category_id=_category(session, "午餐"),
            payee=SECRET_PAYEE if (with_secret_payee and offset == 0) else "",
            note="这是一条可能含敏感信息的备注" if with_secret_payee else "",
        )
    transactions_service.create_transaction(
        session,
        type=TransactionType.INCOME.value,
        account_id=account.id,
        amount_minor=1_000_000,
        occurred_at=datetime.combine(TODAY, datetime.min.time()).replace(hour=9),
    )
    return reports.build_report(session, kind="custom", start=WINDOW_START, end=TODAY)


def _run(coro):
    """在同步用例里跑异步函数。

    刻意不引入 pytest-asyncio：这个项目其余部分全是同步的，
    为了两个用例新增一个插件与一套配置不划算。
    """
    return asyncio.run(coro)


# -----------------------------------------------------------------------------
# 摘要构造与脱敏
# -----------------------------------------------------------------------------
class TestSummary:
    def test_never_contains_payee_or_note(self, session) -> None:
        """**商户名与备注从来不进 payload。**

        不是"默认脱敏所以小心"，而是根本不构造这些字段 ——
        一个不存在的字段不可能被漏脱敏。
        """
        document = _seed_report(session)
        for redact in (True, False):
            blob = json.dumps(ai_service.build_summary(document, redact=redact), ensure_ascii=False)
            assert SECRET_PAYEE not in blob
            assert "敏感信息" not in blob
            assert "note" not in blob
            assert "payee" not in blob

    def test_redacted_has_no_absolute_amounts(self, session) -> None:
        summary = ai_service.build_summary(_seed_report(session), redact=True)
        blob = json.dumps(summary, ensure_ascii=False)
        assert "_minor" not in blob
        assert summary["redacted"] is True
        # 但比例必须在 —— 没有比例就什么也分析不出来
        assert summary["expense_over_income"] is not None

    def test_unredacted_has_absolute_amounts(self, session) -> None:
        summary = ai_service.build_summary(_seed_report(session), redact=False)
        assert summary["redacted"] is False
        assert summary["totals"]["amount_minor"] == 1_000_000

    def test_redaction_replaces_user_chosen_names(self, session) -> None:
        budgets_service.create_budget(
            session,
            name="王小明的生活费",
            scope="total",
            period="yearly",
            amount_minor=5_000_000,
        )
        piggy_service.create_bank(
            session, name="王小明的相机", target_amount_minor=500_000, initial_minor=100_000
        )
        document = _seed_report(session)

        redacted = ai_service.build_summary(document, redact=True)
        blob = json.dumps(redacted, ensure_ascii=False)
        assert "王小明" not in blob
        assert any(item["name"].startswith("预算") for item in redacted.get("budgets", []))
        assert any(item["name"].startswith("罐子") for item in redacted.get("piggy_banks", []))
        # 账户名也换掉（账户名常含银行与尾号）
        assert all(item["name"].startswith("账户") for item in redacted.get("accounts", []))

        plain = json.dumps(ai_service.build_summary(document, redact=False), ensure_ascii=False)
        assert "王小明" in plain, "不脱敏时应当保留真实名称"

    def test_category_names_are_kept(self, session) -> None:
        """**分类名保留** —— 它们来自内置字典，不含个人信息，
        而它们正是让分析有用的东西。

        这条边界写进用例，是为了避免"脱敏"滑向
        "把一切替换成 A/B/C，于是分析什么也说不出来"。
        """
        summary = ai_service.build_summary(_seed_report(session), redact=True)
        names = [item["category"] for item in summary.get("expense_breakdown", [])]
        assert "午餐" in names

    def test_zero_data_summary_is_buildable(self, session) -> None:
        document = reports.build_report(session, kind="monthly", today=TODAY)
        summary = ai_service.build_summary(document, redact=True)
        assert summary["period"]["days"] >= 1
        assert summary["expense_over_income"] is None

    def test_insight_evidence_is_dropped(self, session) -> None:
        """规则洞察的 `evidence` 字符串里嵌着原始金额，必须丢掉。"""
        document = _seed_report(session)
        summary = ai_service.build_summary(document, redact=True)
        for item in summary.get("rule_based_flags", []):
            assert "evidence" not in item
        assert "_minor" not in json.dumps(summary, ensure_ascii=False)


# -----------------------------------------------------------------------------
# 离线回落
# -----------------------------------------------------------------------------
class TestOfflineFallback:
    def test_disabled_returns_offline(self, session) -> None:
        document = _seed_report(session)
        result = _run(ai_service.analyze(session, document))
        assert result["source"] == "offline"
        assert result["fallback_reason"] == "disabled"
        assert result["content"], "**永远要有内容可显示**"

    def test_no_key_returns_offline(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True)
        result = _run(ai_service.analyze(session, _seed_report(session)))
        assert result["source"] == "offline"
        assert result["fallback_reason"] == "no_key"
        assert "没有联网" in result["content"]

    def test_offline_content_mentions_the_numbers(self, session) -> None:
        """离线分析不是残次品：它给出真实的收支与占比。"""
        result = _run(ai_service.analyze(session, _seed_report(session)))
        assert "收入" in result["content"] and "支出" in result["content"]
        assert "支出占收入的" in result["content"]

    def test_offline_on_zero_data_says_so(self, session) -> None:
        """零数据时直说"无法分析"，而不是硬凑一段空话。"""
        document = reports.build_report(session, kind="monthly", today=TODAY)
        result = _run(ai_service.analyze(session, document))
        assert "没有任何收支记录" in result["content"]

    def test_network_error_falls_back(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test")

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("网络不可达", request=request)

        result = _run(ai_service.analyze(session, _seed_report(session), transport=httpx.MockTransport(boom)))
        assert result["source"] == "offline"
        assert result["fallback_reason"] == "network"
        assert result["content"]

    def test_timeout_falls_back(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test")

        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("超时", request=request)

        result = _run(ai_service.analyze(session, _seed_report(session), transport=httpx.MockTransport(slow)))
        assert result["fallback_reason"] == "timeout"

    def test_server_error_falls_back_without_echoing_body(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test")

        def failing(request: httpx.Request) -> httpx.Response:
            # 错误体里回显请求内容是很常见的；我们只记状态码
            return httpx.Response(500, json={"error": "internal", "echo": SECRET_PAYEE})

        result = _run(
            ai_service.analyze(session, _seed_report(session), transport=httpx.MockTransport(failing))
        )
        assert result["fallback_reason"] == "server"
        assert result["error"] == "HTTP 500"
        # 只针对 error 字段断言：离线分析内容是**本地**渲染的，
        # 里面出现用户自己的商户名是正常的（他看的是自己的数据，
        # 而且离线内容根本不会发出去）。要防的是把服务端响应体回显进来。
        assert SECRET_PAYEE not in result["error"]
        assert "echo" not in result["error"]

    def test_bad_response_falls_back(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test")

        def empty(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        result = _run(
            ai_service.analyze(session, _seed_report(session), transport=httpx.MockTransport(empty))
        )
        assert result["fallback_reason"] == "network"
        assert result["content"]


# -----------------------------------------------------------------------------
# 联网路径（MockTransport，不联网、不需要真 key）
# -----------------------------------------------------------------------------
class TestOnlinePath:
    def _transport(self, captured: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization", "")
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "模型给出的分析。"}}]},
            )

        return httpx.MockTransport(handler)

    def test_online_result_and_request_shape(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-secret", model="deepseek-chat")
        captured: dict = {}
        result = _run(ai_service.analyze(session, _seed_report(session), transport=self._transport(captured)))
        assert result["source"] == "online"
        assert result["content"] == "模型给出的分析。"
        assert result["model"] == "deepseek-chat"
        # 端点拼装与鉴权头
        assert captured["url"].endswith("/chat/completions")
        assert captured["auth"] == "Bearer sk-secret"
        assert captured["body"]["model"] == "deepseek-chat"
        assert captured["body"]["stream"] is False
        assert captured["body"]["messages"][0]["role"] == "system"

    def test_payload_sent_to_model_is_the_redacted_summary(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-secret", redact=True)
        captured: dict = {}
        _run(ai_service.analyze(session, _seed_report(session), transport=self._transport(captured)))
        user_text = captured["body"]["messages"][1]["content"]
        assert SECRET_PAYEE not in user_text
        assert "_minor" not in user_text
        assert '"redacted": true' in user_text

    def test_unredacted_payload_carries_amounts(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True, api_key="sk-secret", redact=False)
        captured: dict = {}
        _run(ai_service.analyze(session, _seed_report(session), transport=self._transport(captured)))
        user_text = captured["body"]["messages"][1]["content"]
        assert '"redacted": false' in user_text
        assert "1000000" in user_text
        # 即便如此，单笔商户名仍然不出现 —— 摘要里根本没有这些字段
        assert SECRET_PAYEE not in user_text


# -----------------------------------------------------------------------------
# SSE
# -----------------------------------------------------------------------------
class TestStreaming:
    def _collect(self, session, document) -> list[str]:
        async def run() -> list[str]:
            return [frame async for frame in ai_service.stream_analyze(session, document)]

        return _run(run())

    def test_frames_start_with_meta_and_end_with_done(self, session) -> None:
        frames = self._collect(session, _seed_report(session))
        assert frames[0].startswith("event: meta")
        assert frames[-1].startswith("event: done")

    def test_deltas_reassemble_to_the_content(self, session) -> None:
        # `_seed_report` 有副作用（每次调用都会新建流水），
        # 因此文档只能建一次 —— 建两次会让"期望值"基于另一份数据
        document = _seed_report(session)
        frames = self._collect(session, document)
        text = "".join(
            json.loads(frame.split("data: ", 1)[1])["text"]
            for frame in frames
            if frame.startswith("event: delta")
        )
        assert text == ai_service.offline_analysis(document)

    def test_cjk_is_not_escaped(self, session) -> None:
        """`ensure_ascii=False`：默认会把中文转成 `\\uXXXX`，
        浏览器能解析，但**抓包或看日志时完全不可读**，
        而调试流式接口时人正是靠肉眼看这些帧。"""
        frames = self._collect(session, _seed_report(session))
        joined = "".join(frames)
        assert "收入" in joined
        assert "\\u" not in joined

    def test_chunking_never_splits_a_frame(self, session) -> None:
        """每帧必须以空行结束 —— 否则前端的 SSE 解析器会一直等下一块。"""
        frames = self._collect(session, _seed_report(session))
        assert all(frame.endswith("\n\n") for frame in frames)

    def test_meta_carries_the_source(self, session) -> None:
        frames = self._collect(session, _seed_report(session))
        meta = json.loads(frames[0].split("data: ", 1)[1])
        assert meta["source"] == "offline"
        assert meta["fallback_reason"] == "disabled"
        assert meta["analysis_id"]

    def test_online_stream_goes_through_the_same_frames(self, session) -> None:
        """离线与联网走**同一套帧**，前端因此只有一条渲染路径。"""
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "在线分析内容"}}]})

        async def run() -> list[str]:
            return [
                frame
                async for frame in ai_service.stream_analyze(
                    session, _seed_report(session), transport=httpx.MockTransport(handler)
                )
            ]

        frames = _run(run())
        meta = json.loads(frames[0].split("data: ", 1)[1])
        assert meta["source"] == "online"
        text = "".join(
            json.loads(frame.split("data: ", 1)[1])["text"]
            for frame in frames
            if frame.startswith("event: delta")
        )
        assert text == "在线分析内容"


# -----------------------------------------------------------------------------
# 配置
# -----------------------------------------------------------------------------
class TestConfig:
    def test_defaults_are_protective(self, session) -> None:
        """默认值应当是保护性的：没配过时**不启用**、且**默认脱敏**。"""
        config = ai_service.get_ai_config(session)
        assert config.enabled is False
        assert config.redact is True
        assert config.has_key is False
        assert config.usable is False

    def test_api_key_is_three_state(self, session) -> None:
        ai_service.save_ai_config(session, api_key="sk-one")
        assert ai_service.get_ai_config(session).api_key == "sk-one"
        # 只改模型时不该把 key 清掉
        ai_service.save_ai_config(session, model="other-model")
        assert ai_service.get_ai_config(session).api_key == "sk-one"
        # 空串才是清除 —— 否则用户没有办法删掉一个填错的 key
        ai_service.save_ai_config(session, api_key="")
        assert ai_service.get_ai_config(session).api_key == ""

    def test_base_url_must_be_http(self, session) -> None:
        with pytest.raises(ValidationError, match="http"):
            ai_service.save_ai_config(session, base_url="api.example.com")

    def test_timeout_is_bounded(self, session) -> None:
        with pytest.raises(ValidationError, match="5–300"):
            ai_service.save_ai_config(session, timeout_seconds=1)

    def test_usable_requires_both_enabled_and_key(self, session) -> None:
        ai_service.save_ai_config(session, enabled=True)
        assert ai_service.get_ai_config(session).usable is False
        ai_service.save_ai_config(session, api_key="sk-x")
        assert ai_service.get_ai_config(session).usable is True


# -----------------------------------------------------------------------------
# 存档
# -----------------------------------------------------------------------------
class TestArchiving:
    def test_offline_analysis_is_archived(self, session) -> None:
        """离线分析也要存档：它是"这一期看过什么"的记录。"""
        result = _run(ai_service.analyze(session, _seed_report(session)))
        assert result["analysis_id"]
        rows = ai_service.list_analyses(session)
        assert len(rows) == 1
        assert rows[0].source == "offline"
        assert rows[0].fallback_reason == "disabled"

    def test_archived_payload_matches_redaction_setting(self, session) -> None:
        """存下来的是**实际发出去的**摘要，将来才能回答"这次分析基于什么数据"。"""
        ai_service.save_ai_config(session, enabled=True, api_key="sk-test", redact=True)
        _run(ai_service.analyze(session, _seed_report(session)))
        row = ai_service.list_analyses(session)[0]
        assert row.redacted is True
        assert "_minor" not in json.dumps(row.payload, ensure_ascii=False)

    def test_record_can_be_disabled(self, session) -> None:
        _run(ai_service.analyze(session, _seed_report(session), record=False))
        assert ai_service.list_analyses(session) == []

    def test_serialize_has_no_secret(self, session) -> None:
        _run(ai_service.analyze(session, _seed_report(session)))
        payload = ai_service.serialize_analysis(ai_service.list_analyses(session)[0])
        assert "api_key" not in json.dumps(payload)
        assert payload["content"]
