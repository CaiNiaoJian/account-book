"""P5 的 API 路由：台账与报表（含三种格式的导出）。

导出端点为什么返回 `Response` 而不是 JSON
=========================================
导出的是**文件**：浏览器要按 `Content-Disposition` 下载，
PDF 要能被 PDF 阅读器直接打开。用 JSON 包一层 base64 会让前端
多一次解码、多一次内存拷贝（一份年报 PDF 可能是几 MB），
而且用户右键"另存为"时会得到一个 `.json`。

三种格式都显式带上 `charset=utf-8`：Markdown 与 HTML 里全是中文，
不写字符集时浏览器会按本地代码页猜，结果是满屏乱码。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ...core.errors import ValidationError
from ...services import ledger as ledger_service
from ...services import report_export, reports
from ..deps import get_session
from ..schemas import (
    LedgerOut,
    ReconcileOut,
    ReconcileRequest,
    ReportOut,
    TrialBalanceOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["reporting"])

SessionDep = Depends(get_session)

#: 导出格式 → (媒体类型, 扩展名)
_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
}


def _parse_include(include: str | None) -> set[str] | None:
    """``?include=overview,breakdown`` → ``{"overview","breakdown"}``。

    也接受重复参数（``?include=a&include=b``）拼出来的逗号串 ——
    前端两种写法都可能用上，统一在这里处理。
    """
    if not include:
        return None
    keys = {part.strip() for part in include.split(",") if part.strip()}
    if not keys:
        return None
    unknown = keys - set(reports.section_keys())
    if unknown:
        raise ValidationError(
            f"未知的报告小节：{'、'.join(sorted(unknown))}",
            field="include",
            allowed=reports.section_keys(),
        )
    return keys


# -----------------------------------------------------------------------------
# 台账
# -----------------------------------------------------------------------------
# 注意顺序：字面量路由必须声明在 `/ledger/{account_id}` **之前**。
# FastAPI 按声明顺序匹配，路径参数路由在前时 `trial-balance`
# 会被当成 account_id="trial-balance"，于是这个端点永远返回 422。
@router.get("/ledger/trial-balance", response_model=TrialBalanceOut, summary="试算平衡")
def get_trial_balance(session: Session = SessionDep) -> Any:
    """全局自洽校验。任何"漏算了一个方向"的 bug 都会让它失败。"""
    return ledger_service.trial_balance(session)


@router.get("/ledger/{account_id}", response_model=LedgerOut, summary="账户台账")
def get_ledger(
    account_id: int,
    start: date = Query(...),
    end: date = Query(...),
    session: Session = SessionDep,
) -> Any:
    """期初 + 逐笔（含滚动余额）+ 期末 + 连续性校验。"""
    return ledger_service.build_ledger(session, account_id, start=start, end=end)


@router.post(
    "/ledger/{account_id}/reconcile",
    response_model=ReconcileOut,
    summary="对账",
)
def reconcile(
    account_id: int,
    payload: ReconcileRequest,
    session: Session = SessionDep,
) -> Any:
    """拿真实余额与台账比，差多少补一笔调整分录。

    `create_adjustment=False` 时只算差异不落库 —— 界面需要能先给用户看。
    """
    return ledger_service.reconcile(session, account_id, **payload.model_dump())


# -----------------------------------------------------------------------------
# 报表
# -----------------------------------------------------------------------------
@router.get("/reports/section-keys", summary="可选的报告小节")
def list_section_keys() -> Any:
    return {"items": reports.section_keys()}


@router.get("/reports", response_model=ReportOut, summary="构建报告")
def build_report(
    kind: str = Query(default="monthly"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    include: str | None = Query(default=None, description="逗号分隔的小节名"),
    session: Session = SessionDep,
) -> Any:
    """返回 `ReportDocument`。

    路由层不缓存：报告依赖"今天"（区间、完整度、预计达成日），
    缓存住会让第二天打开时看到昨天的口径。
    """
    return reports.build_report(session, kind=kind, start=start, end=end, include=_parse_include(include))


@router.get("/reports/export", summary="导出报告")
def export_report(
    kind: str = Query(default="monthly"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    include: str | None = Query(default=None),
    format: str = Query(default="markdown", description="markdown / html / pdf"),
    inline: bool = Query(default=False, description="为真时在浏览器里内联打开"),
    session: Session = SessionDep,
) -> Response:
    """导出为可下载的文件。

    `inline=True` 时不带 `attachment` —— HTML 与 PDF 可以直接在
    浏览器/阅读器里打开，而 Markdown 更应该被存下来。
    """
    key = (format or "").strip().lower()
    if key not in _EXPORT_FORMATS:
        raise ValidationError(f"未知的导出格式：{format}", field="format", allowed=sorted(_EXPORT_FORMATS))
    media_type, extension = _EXPORT_FORMATS[key]
    document = reports.build_report(session, kind=kind, start=start, end=end, include=_parse_include(include))

    if key == "pdf":
        payload: bytes = report_export.render_pdf(document)
    else:
        text = (
            report_export.render_markdown(document)
            if extension == "md"
            else report_export.render_html(document)
        )
        # 带 BOM：Windows 上记事本按本地代码页打开无 BOM 的 UTF-8 文件会乱码，
        # 而这个应用的导出文件很可能就是被双击打开的
        payload = text.encode("utf-8-sig") if extension == "md" else text.encode("utf-8")

    filename = f"report-{document['kind']}-{document['period']['start']}.{extension}"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            # 报告依赖"今天"，不该被任何一层缓存住
            "Cache-Control": "no-store",
        },
        status_code=status.HTTP_200_OK,
    )
