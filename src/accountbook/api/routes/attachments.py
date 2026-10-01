"""附件路由（P1 尾巴 T5 / T6）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from ...core.errors import ValidationError
from ...services import attachments as attachments_service
from ..deps import get_session
from ..schemas import AttachmentOut

__all__ = ["router"]

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

SessionDep = Depends(get_session)


def _root(request: Request) -> Path:
    """附件根目录由装配层提供，服务层不自己去查全局路径。

    这样服务层能在临时目录里被测试，而"附件写到哪"由启动装配决定。
    """
    return request.app.state.ctx.paths.attachments


@router.post(
    "/transactions/{transaction_id}",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="给流水上传附件",
)
async def upload_for_transaction(
    transaction_id: int,
    request: Request,
    filename: str = Query(default="attachment", max_length=200),
    session: Session = SessionDep,
) -> Any:
    """上传附件。

    **用原始字节体而不是 multipart**：前端只要 `fetch(url, { body: file })`，
    测试只要 `client.post(url, content=b"...")`，也少一个
    python-multipart 的依赖风险。

    先看 `Content-Length` 做一次快速拒绝（避免白读上百 MB），
    真正的上限在服务层按实际字节数再判一次 —— 前者挡君子，后者是防线。
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            attachments_service.assert_size_within_limit(int(declared))
        except ValueError as error:
            raise ValidationError("Content-Length 不是数字", field="content-length") from error

    data = await request.body()
    attachment = attachments_service.save(
        session,
        _root(request),
        data,
        filename=filename,
        kind="transaction",
        transaction_id=transaction_id,
    )
    return attachments_service.serialize(attachment)


@router.post(
    "/card-artworks/{artwork_id}",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="给卡面上传图片",
)
async def upload_for_artwork(
    artwork_id: int,
    request: Request,
    filename: str = Query(default="card.png", max_length=200),
    session: Session = SessionDep,
) -> Any:
    """卡面图片。与流水附件共用同一套存储与校验（见 services/attachments.py）。"""
    data = await request.body()
    attachment = attachments_service.save(
        session,
        _root(request),
        data,
        filename=filename,
        kind="card",
        card_artwork_id=artwork_id,
    )
    return attachments_service.serialize(attachment)


@router.get("/transactions/{transaction_id}", response_model=list[AttachmentOut], summary="流水的附件")
def list_for_transaction(transaction_id: int, session: Session = SessionDep) -> list[Any]:
    return [
        attachments_service.serialize(item)
        for item in attachments_service.list_for_transaction(session, transaction_id)
    ]


@router.get("/{attachment_id}", summary="读取附件内容")
def download(attachment_id: int, request: Request, session: Session = SessionDep) -> Response:
    """返回文件内容。

    走同源请求，因此浏览器的会话 Cookie 会带上 —— `<img src>` 直接可用。
    `Content-Disposition: inline` 让图片能内联显示、PDF 能在查看器里打开。
    """
    attachment = attachments_service.get(session, attachment_id)
    data = attachments_service.read_bytes(_root(request), attachment)
    return Response(
        content=data,
        media_type=attachment.mime,
        headers={
            "Content-Disposition": f'inline; filename="{attachment.original_name}"',
            # 附件内容不可变（内容寻址），可以放心长缓存
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除附件")
def delete(attachment_id: int, request: Request, session: Session = SessionDep) -> None:
    attachments_service.delete(session, _root(request), attachment_id)
