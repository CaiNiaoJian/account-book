"""AI 分析接入（P5 / 需求 21）。

三条贯穿全篇的原则
==================

**一、离线回落是默认路径，不是错误路径。**
没有 key、断网、超时、服务端报错 —— 任何一种情况下用户都必须拿到
**能用的东西**，而不是一块空白加一句"分析失败"。
`services/reports.py` 里的规则洞察本来就是离线可得的，
因此回落只是换一个来源，而不是降级到一个次品。
响应里始终带 `source`（`online` / `offline`）与 `fallback_reason`，
界面据此明说"这次是离线规则分析"—— 让用户以为是模型说的、
实际是几条 if-else，那才是真的欺骗。

**二、永远不发流水明细。**
模型拿到的是**聚合摘要**：期间、收支比例、分类占比、预算进度、
存钱罐进度、登记完整度。商户名、备注、单笔金额**从来不进 payload** ——
不是"默认不脱敏所以小心"，而是**根本不构造**这些字段。
一个不存在的字段不可能被漏脱敏。

**三、脱敏只做两件事，且都可测。**
`redact=True` 时：

* **去掉绝对金额**，只留比例（"餐饮占支出 38%"而不是"餐饮 1,234 元"）；
* **替换用户自取的名字**（预算名、罐子名、账户名 → 预算A / 罐子A / 账户A）。

分类名（餐饮/交通）**保留** —— 它们来自内置字典，不含个人信息，
而它们正是让分析有用的东西。这条边界写在这里，是为了避免
"脱敏"滑向"把一切替换成 A/B/C，于是分析什么也说不出来"。

密钥不进前端、不进日志
======================
API key 存在 `app_settings`（数据侧），接口**只返回 `has_key` 布尔值**，
永远不回传 key 本身。日志里也只记录"有没有 key"。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from ..db.models import AiAnalysis, AppSetting

__all__ = [
    "AI_CONFIG_KEY",
    "AiConfig",
    "analyze",
    "build_summary",
    "get_ai_config",
    "list_analyses",
    "offline_analysis",
    "save_ai_config",
    "serialize_analysis",
    "stream_analyze",
    "stream_frames",
]

_logger = logging.getLogger(__name__)

#: AI 配置在 app_settings 里的键
AI_CONFIG_KEY = "ai_analysis"

#: 默认的 OpenAI 兼容端点（DeepSeek 兼容同一套协议）
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 送进模型的摘要里最多几条分类 / 预算 / 罐子
_MAX_ITEMS = 12

#: 单次请求超时（秒）。分析是交互式的，超过这个时间用户已经在看别的了
DEFAULT_TIMEOUT = 45.0

_SYSTEM_PROMPT = (
    "你是一位克制、诚实的个人财务助手。只会看到**聚合后的**统计数据，"
    "看不到任何单笔流水、商户或备注。请：\n"
    "1. 只依据给定数据说话，不要编造没有出现的数字或事实；\n"
    "2. 指出值得注意的一到三件事，并给出可执行的具体建议；\n"
    "3. 用简体中文，分点，总长度不超过 300 字；\n"
    "4. 如果数据太少不足以判断，就直说数据太少，不要硬凑结论。"
)


@dataclass(slots=True)
class AiConfig:
    """AI 配置。`api_key` 只在服务端流转，绝不返回给前端。"""

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT
    #: 默认**开启**脱敏：默认值应当是保护性的
    redact: bool = True

    @property
    def has_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def usable(self) -> bool:
        return self.enabled and self.has_key


def get_ai_config(session: Session) -> AiConfig:
    row = session.get(AppSetting, AI_CONFIG_KEY)
    if row is None or not isinstance(row.value, dict):
        return AiConfig()
    payload = row.value
    return AiConfig(
        enabled=bool(payload.get("enabled", False)),
        base_url=str(payload.get("base_url") or DEFAULT_BASE_URL),
        model=str(payload.get("model") or DEFAULT_MODEL),
        api_key=str(payload.get("api_key") or ""),
        timeout_seconds=float(payload.get("timeout_seconds") or DEFAULT_TIMEOUT),
        redact=bool(payload.get("redact", True)),
    )


def save_ai_config(session: Session, **changes: Any) -> AiConfig:
    """更新 AI 配置。

    `api_key` 的语义刻意做成三态：
    * 不传 → 保持原值（用户只改模型时不该把 key 清掉）；
    * 传空字符串 → 清除；
    * 传新值 → 替换。

    若用"空字符串就是不变"，用户就**没有办法**删掉一个填错的 key。
    """
    current = get_ai_config(session)
    payload: dict[str, Any] = {
        "enabled": current.enabled,
        "base_url": current.base_url,
        "model": current.model,
        "api_key": current.api_key,
        "timeout_seconds": current.timeout_seconds,
        "redact": current.redact,
    }
    if "enabled" in changes and changes["enabled"] is not None:
        payload["enabled"] = bool(changes["enabled"])
    if "redact" in changes and changes["redact"] is not None:
        payload["redact"] = bool(changes["redact"])
    if changes.get("base_url"):
        url = str(changes["base_url"]).strip()
        if not url.startswith(("http://", "https://")):
            raise ValidationError("接口地址必须以 http:// 或 https:// 开头", field="base_url")
        payload["base_url"] = url.rstrip("/")
    if changes.get("model"):
        payload["model"] = str(changes["model"]).strip()
    if changes.get("timeout_seconds") is not None:
        timeout = float(changes["timeout_seconds"])
        if not 5 <= timeout <= 300:
            raise ValidationError("超时必须在 5–300 秒之间", field="timeout_seconds")
        payload["timeout_seconds"] = timeout
    if "api_key" in changes and changes["api_key"] is not None:
        payload["api_key"] = str(changes["api_key"]).strip()

    row = session.get(AppSetting, AI_CONFIG_KEY)
    if row is None:
        session.add(AppSetting(key=AI_CONFIG_KEY, value=payload))
    else:
        row.value = payload
        # JSON 列是原地修改检测不到的，显式标记为脏
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "value")
    session.flush()
    _logger.info(
        "AI 配置已更新：enabled=%s model=%s has_key=%s redact=%s",
        payload["enabled"],
        payload["model"],
        bool(payload["api_key"]),
        payload["redact"],
    )
    return get_ai_config(session)


# -----------------------------------------------------------------------------
# 摘要构造
# -----------------------------------------------------------------------------
def _ratio(value: float) -> float:
    return round(float(value), 4)


def _name(index: int, prefix: str) -> str:
    """``预算A`` / ``预算B`` …… 超过 26 个就回到 A 重新编号（不会发生，但要有界）"""
    return f"{prefix}{chr(ord('A') + index % 26)}"


def _section(document: dict[str, Any], key: str) -> dict[str, Any]:
    return next((item for item in document.get("sections", []) if item["key"] == key), {})


def build_summary(document: dict[str, Any], *, redact: bool = True) -> dict[str, Any]:
    """把报告压成送进模型的聚合摘要。

    **这个函数是唯一构造 payload 的地方**，因此"什么不会被发出去"
    在这里一眼可见：不读 `payee`、不读 `note`、不读单笔流水。
    一个不存在的字段不可能被漏脱敏。

    `redact=False` 时额外给出绝对金额与真实名称 ——
    即便如此也**仍然不含单笔明细**。
    """
    period = document.get("period", {})
    kpis = {item["key"]: item for item in document.get("kpis", [])}
    income = int(kpis.get("income", {}).get("value_minor", 0))
    expense = int(kpis.get("expense", {}).get("value_minor", 0))

    def money(item: dict[str, Any]) -> dict[str, Any]:
        """金额字段：脱敏时只给比例，不脱敏时给绝对值。"""
        amount = int(item.get("value_minor", 0))
        if not redact:
            return {"amount_minor": amount}
        base = income if income else (expense or 1)
        return {"ratio_of_income": _ratio(amount / base) if base else 0.0}

    summary: dict[str, Any] = {
        "period": {
            "kind": document.get("kind"),
            "start": period.get("start"),
            "end": period.get("end"),
            "days": period.get("days"),
        },
        "totals": {
            **money(kpis.get("income", {})),
            "expense": money(kpis.get("expense", {})),
            "net": money(kpis.get("net", {})),
        },
        # 收支比本身不含绝对金额，脱敏与否都给 —— 没有它分析就无从谈起
        "expense_over_income": _ratio(expense / income) if income else None,
        "kpis": [
            {
                "key": item["key"],
                "label": item.get("label", ""),
                "kind": item.get("kind"),
                "delta_ratio": item.get("delta_ratio"),
                **money(item),
            }
            for item in document.get("kpis", [])
        ],
    }

    # 分类构成：分类名来自内置字典，不含个人信息，**保留**
    breakdown = _section(document, "breakdown")
    for block in breakdown.get("blocks", []):
        if block.get("type") != "table":
            continue
        title = block.get("title", "")
        if "支出明细" not in title and "收入明细" not in title:
            continue
        rows = block.get("rows", [])[:_MAX_ITEMS]
        key = "expense_breakdown" if "支出" in title else "income_breakdown"
        summary[key] = [
            {"category": row.get("name", ""), "share": _ratio(float(row.get("share", 0)))} for row in rows
        ]

    # 预算 / 罐子 / 账户：名字是用户自取的 → 脱敏时替换
    budgets = []
    for block in _section(document, "budgets").get("blocks", []):
        if block.get("type") != "table":
            continue
        for index, row in enumerate(block.get("rows", [])[:_MAX_ITEMS]):
            entry = {
                "name": row.get("name", "") if not redact else _name(index, "预算"),
                "ratio": _ratio(float(row.get("ratio", 0))),
            }
            if not redact:
                entry["spent_minor"] = int(row.get("spent_minor", 0))
                entry["budget_minor"] = int(row.get("budget_minor", 0))
            budgets.append(entry)
    if budgets:
        summary["budgets"] = budgets

    piggy = []
    for block in _section(document, "piggy").get("blocks", []):
        if block.get("type") != "table":
            continue
        for index, row in enumerate(block.get("rows", [])[:_MAX_ITEMS]):
            entry = {
                "name": row.get("name", "") if not redact else _name(index, "罐子"),
                "ratio": _ratio(float(row.get("ratio", 0))),
                "status": row.get("status", ""),
            }
            if not redact:
                entry["balance_minor"] = int(row.get("balance_minor", 0))
                entry["target_amount_minor"] = int(row.get("target_amount_minor", 0))
            piggy.append(entry)
    if piggy:
        summary["piggy_banks"] = piggy

    # 账户：只给变动方向与占比，不给名字（账户名常含银行与尾号）
    accounts = []
    for block in _section(document, "accounts").get("blocks", []):
        if block.get("type") != "table":
            continue
        rows = block.get("rows", [])
        total = sum(abs(int(row.get("change_minor", 0))) for row in rows) or 1
        for index, row in enumerate(rows[:_MAX_ITEMS]):
            change = int(row.get("change_minor", 0))
            entry: dict[str, Any] = {
                "name": row.get("name", "") if not redact else _name(index, "账户"),
                "change_ratio_of_total": _ratio(abs(change) / total),
                "direction": "in" if change >= 0 else "out",
            }
            if not redact:
                entry["change_minor"] = change
                entry["closing_minor"] = int(row.get("closing_minor", 0))
            accounts.append(entry)
    if accounts:
        summary["accounts"] = accounts

    # 登记完整度：比例本身不敏感
    for block in _section(document, "calendar").get("blocks", []):
        if block.get("type") != "metrics":
            continue
        for item in block.get("items", []):
            if item.get("key") == "completeness":
                summary["recording_completeness"] = _ratio(float(item.get("value", 0)))

    # 规则洞察：给标题与等级，但**丢掉 evidence**（那些字符串里嵌着原始金额）
    if document.get("insights"):
        summary["rule_based_flags"] = [
            {"level": item.get("level"), "title": item.get("title")} for item in document["insights"][:8]
        ]

    summary["redacted"] = bool(redact)
    return summary


# -----------------------------------------------------------------------------
# 离线分析（回落路径，也是默认路径）
# -----------------------------------------------------------------------------
_LEVEL_MARK = {"critical": "【需立即处理】", "warn": "【需注意】", "info": "【可留意】", "good": "【良好】"}


def offline_analysis(document: dict[str, Any]) -> str:
    """离线规则分析。

    它**不是**"模型不可用时的残次品"：报告里的洞察本来就是规则算出来的，
    这里只是把它们组织成一段可读的分析。断网时用户拿到的信息量
    与联网时并没有数量级差别 —— 只是少了模型的语言组织。
    """
    period = document.get("period", {})
    kpis = {item["key"]: item for item in document.get("kpis", [])}
    income = int(kpis.get("income", {}).get("value_minor", 0))
    expense = int(kpis.get("expense", {}).get("value_minor", 0))
    net = int(kpis.get("net", {}).get("value_minor", 0))

    lines: list[str] = [
        f"**{period.get('label') or period.get('start', '')}**（离线规则分析）",
        "",
    ]
    if income == 0 and expense == 0:
        lines.append("这一期没有任何收支记录，暂时无法给出分析。")
        lines.append("")
        lines.append("先记几笔账，或把报表区间换到有数据的时间段。")
        return "\n".join(lines)

    lines.append(
        f"收入 {income / 100:.2f} 元，支出 {expense / 100:.2f} 元，"
        f"{'结余' if net >= 0 else '净流出'} {abs(net) / 100:.2f} 元。"
    )
    if income:
        lines.append(f"支出占收入的 {expense / income * 100:.0f}%。")
    lines.append("")

    insights = document.get("insights", [])
    if insights and not (len(insights) == 1 and insights[0].get("key") == "nothing_notable"):
        lines.append("**值得注意的几点**")
        lines.append("")
        for item in insights:
            mark = _LEVEL_MARK.get(item.get("level", ""), "")
            lines.append(f"- {mark}{item.get('title', '')} —— {item.get('detail', '')}")
            if item.get("suggestion"):
                lines.append(f"  - 建议：{item['suggestion']}")
    else:
        lines.append("收支结构、预算执行与登记完整度都在合理范围内，没有发现需要特别关注的地方。")

    lines.append("")
    lines.append("> 以上结论由**离线规则**得出，没有联网、没有调用任何模型。")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# 联网分析
# -----------------------------------------------------------------------------
def _messages(document: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"请分析这份《{document.get('title', '财务报告')}》。\n"
                f"以下是聚合统计数据（JSON）：\n```json\n"
                f"{json.dumps(summary, ensure_ascii=False, indent=2)}\n```"
            ),
        },
    ]


async def _call_model(
    config: AiConfig,
    messages: list[dict[str, str]],
    *,
    stream: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    """调用 OpenAI 兼容端点。

    `transport` 只为测试而存在：有了它，可以在**不联网、不需要真 key**
    的情况下验证请求体与 SSE 解析 —— 否则这条路径只能靠"人工试一次"，
    而那种验证在 CI 里等于没有。
    """
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.model,
        "messages": messages,
        "stream": stream,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=config.timeout_seconds, transport=transport) as client:
        response = await client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        if not stream:
            body = response.json()
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("模型返回里没有 choices")
            return str(choices[0].get("message", {}).get("content", "")).strip()
        return response


def _fallback(document: dict[str, Any], reason: str, *, error: str = "") -> dict[str, Any]:
    return {
        "source": "offline",
        "fallback_reason": reason,
        "error": error,
        "content": offline_analysis(document),
        "model": "",
    }


async def analyze(
    session: Session,
    document: dict[str, Any],
    *,
    config: AiConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    record: bool = True,
) -> dict[str, Any]:
    """分析一份报告。

    **任何异常都回落到离线分析**，并且把原因如实写在 `fallback_reason` 里
    （`no_key` / `disabled` / `network` / `timeout` / `server` / `bad_response`）。
    调用方永远能拿到 `content`，因此界面永远有东西可显示。
    """
    config = config or get_ai_config(session)
    if not config.enabled:
        return _remember(session, document, _fallback(document, "disabled"), config, record)
    if not config.has_key:
        return _remember(session, document, _fallback(document, "no_key"), config, record)

    summary = build_summary(document, redact=config.redact)
    try:
        content = await _call_model(config, _messages(document, summary), stream=False, transport=transport)
    except httpx.TimeoutException as exc:
        return _remember(
            session, document, _fallback(document, "timeout", error=str(exc)[:180]), config, record
        )
    except httpx.HTTPStatusError as exc:
        # 别把响应体整段塞进去：里面可能回显请求内容
        return _remember(
            session,
            document,
            _fallback(document, "server", error=f"HTTP {exc.response.status_code}"),
            config,
            record,
        )
    except (httpx.HTTPError, ValueError) as exc:
        return _remember(
            session, document, _fallback(document, "network", error=str(exc)[:180]), config, record
        )

    result = {
        "source": "online",
        "fallback_reason": "",
        "error": "",
        "content": content,
        "model": config.model,
    }
    return _remember(session, document, result, config, record)


def _remember(
    session: Session,
    document: dict[str, Any],
    result: dict[str, Any],
    config: AiConfig,
    record: bool,
) -> dict[str, Any]:
    """存档。**只存脱敏后的 payload**（若开启脱敏）。"""
    if record:
        period = document.get("period", {})
        row = AiAnalysis(
            report_kind=str(document.get("kind", "")),
            period_start=date.fromisoformat(str(period.get("start"))),
            period_end=date.fromisoformat(str(period.get("end"))),
            source=result["source"],
            model=result.get("model", ""),
            redacted=config.redact,
            payload=build_summary(document, redact=config.redact),
            content=result["content"],
            fallback_reason=result.get("fallback_reason", ""),
            error=result.get("error", ""),
        )
        session.add(row)
        session.flush()
        result["analysis_id"] = row.id
    return result


# -----------------------------------------------------------------------------
# SSE
# -----------------------------------------------------------------------------
def sse_frame(event: str, data: dict[str, Any]) -> str:
    """SSE 帧。

    `ensure_ascii=False` 是必须的：默认会把中文转成 `\\uXXXX`，
    虽然浏览器能解析，但**抓包或看日志时完全不可读**，
    而调试流式接口时人正是靠肉眼看这些帧。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_frames(result: dict[str, Any], *, chunk_size: int = 24) -> AsyncIterator[str]:
    """把一段文本切成帧推给前端。

    **离线回落也走同一套帧**：前端因此只有一条渲染路径。
    如果离线时返回一个完整的 JSON、联网时返回流，前端就要写两套 ——
    而两套里必然有一套是少测的。
    """
    yield sse_frame(
        "meta",
        {
            "source": result["source"],
            "model": result.get("model", ""),
            "fallback_reason": result.get("fallback_reason", ""),
            "error": result.get("error", ""),
            "analysis_id": result.get("analysis_id"),
        },
    )
    text = result["content"]
    # 按字符切而不是按字节：按字节切会把一个汉字切成两半，
    # 于是流式渲染时会出现一个乱码方块（前端拼回来才恢复）
    for start in range(0, len(text), chunk_size):
        yield sse_frame("delta", {"text": text[start : start + chunk_size]})
    yield sse_frame("done", {"length": len(text)})


async def stream_analyze(
    session: Session,
    document: dict[str, Any],
    *,
    config: AiConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[str]:
    """分析的 SSE 流。**永远以 `meta` 开头**，前端据此先显示来源。"""
    result = await analyze(session, document, config=config, transport=transport)
    async for frame in stream_frames(result):
        yield frame


def list_analyses(session: Session, *, limit: int = 50) -> list[AiAnalysis]:
    from sqlalchemy import select

    return list(session.scalars(select(AiAnalysis).order_by(AiAnalysis.id.desc()).limit(limit)).all())


def serialize_analysis(row: AiAnalysis) -> dict[str, Any]:
    return {
        "id": row.id,
        "report_kind": row.report_kind,
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "source": row.source,
        "model": row.model,
        "redacted": row.redacted,
        "content": row.content,
        "fallback_reason": row.fallback_reason,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _now() -> datetime:  # pragma: no cover - 仅为可读性保留
    return datetime.now()
