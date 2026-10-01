"""AI 分析路由（P5）。

`POST /api/reports/ai-analyze` 返回 **SSE 流**，而不是等模型跑完再返回一个
JSON：模型要几秒到几十秒，期间界面什么都不显示会让人以为卡死了。
而离线回落**也走同一套帧** —— 前端因此只有一条渲染路径。
如果离线返回完整 JSON、联网返回流，前端就要写两套，而两套里必然有一套少测。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...services import ai as ai_service
from ...services import reports
from ..deps import get_session
from ..schemas import AiAnalysisListOut, AiConfigOut, AiConfigUpdate

__all__ = ["router"]

router = APIRouter(prefix="/api/reports", tags=["reporting"])

SessionDep = Depends(get_session)


@router.get("/ai-config", response_model=AiConfigOut, summary="读取 AI 配置")
def get_ai_config(session: Session = SessionDep) -> Any:
    """**只返回 `has_key`，永远不回传密钥本身。**

    界面需要知道"填过没有"，不需要知道"填的是什么"。
    一个能被读回来的密钥，会出现在每一次前端日志、截图与录屏里。
    """
    config = ai_service.get_ai_config(session)
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "redact": config.redact,
        "has_key": config.has_key,
    }


@router.put("/ai-config", response_model=AiConfigOut, summary="更新 AI 配置")
def update_ai_config(payload: AiConfigUpdate, session: Session = SessionDep) -> Any:
    """`api_key` 三态：不传=保持，空串=清除，有值=替换。

    若把"空串"当成"不变"，用户就**没有办法删掉**一个填错的 key。
    """
    ai_service.save_ai_config(session, **payload.model_dump(exclude_unset=True))
    config = ai_service.get_ai_config(session)
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "redact": config.redact,
        "has_key": config.has_key,
    }


@router.post("/ai-analyze", summary="分析报告（SSE 流）")
async def ai_analyze(
    kind: str = Query(default="monthly"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    session: Session = SessionDep,
) -> StreamingResponse:
    document = reports.build_report(session, kind=kind, start=start, end=end)
    return StreamingResponse(
        ai_service.stream_analyze(session, document),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # 关掉反向代理的缓冲，否则流会被攒成一整块再发
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ai-analyses", response_model=AiAnalysisListOut, summary="历史分析")
def list_analyses(limit: int = Query(default=20, ge=1, le=100), session: Session = SessionDep) -> Any:
    rows = ai_service.list_analyses(session, limit=limit)
    return {"items": [ai_service.serialize_analysis(row) for row in rows], "count": len(rows)}
