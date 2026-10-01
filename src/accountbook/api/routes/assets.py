"""资产卡片墙与卡面管理路由（REQ-15）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...services import assets as assets_service
from ..deps import get_session
from ..schemas import (
    CardArtworkCreate,
    CardArtworkOut,
    CardArtworkUpdate,
    CardUpdate,
    InstitutionCreate,
    InstitutionOut,
    InstitutionUpdate,
    ReorderRequest,
)

__all__ = ["router"]

router = APIRouter(tags=["assets"])

SessionDep = Depends(get_session)


@router.get("/api/assets/wall", summary="卡片墙（分组卡片 + 资产概览 + 卡面字典）")
def wall(
    include_archived: bool = Query(default=False),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """一次返回卡片墙所需的全部内容。

    刻意不拆成三个接口：卡片墙是一个整体视图，分三批到达会让卡片先出现、
    颜色后到位，看起来像加载失败过。
    """
    return assets_service.wall(session, include_archived=include_archived)


@router.post("/api/assets/reorder", summary="重排账户（拖拽排序）")
def reorder(payload: ReorderRequest, session: Session = SessionDep) -> dict[str, int]:
    count = assets_service.reorder_accounts(session, payload.order)
    return {"reordered": count}


@router.patch("/api/assets/accounts/{account_id}/card", summary="更新卡片外观")
def update_card(
    account_id: int,
    payload: CardUpdate,
    session: Session = SessionDep,
) -> dict[str, Any]:
    account = assets_service.update_account_cards(
        session, account_id, **payload.model_dump(exclude_unset=True)
    )
    return {"id": account.id, "card_style": account.card_style, "brand_key": account.brand_key}


# -----------------------------------------------------------------------------
# 机构字典
# -----------------------------------------------------------------------------
@router.get("/api/institutions", response_model=list[InstitutionOut], summary="机构列表")
def list_institutions(session: Session = SessionDep) -> list[InstitutionOut]:
    return [
        InstitutionOut.model_validate(assets_service.serialize_institution(item))
        for item in assets_service.list_institutions(session)
    ]


@router.post(
    "/api/institutions",
    response_model=InstitutionOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增机构",
)
def create_institution(payload: InstitutionCreate, session: Session = SessionDep) -> InstitutionOut:
    institution = assets_service.create_institution(
        session,
        key=payload.key,
        name=payload.name,
        kind=payload.kind,
        brand_color=payload.brand_color,
    )
    return InstitutionOut.model_validate(assets_service.serialize_institution(institution))


@router.patch("/api/institutions/{institution_id}", response_model=InstitutionOut, summary="更新机构")
def update_institution(
    institution_id: int,
    payload: InstitutionUpdate,
    session: Session = SessionDep,
) -> InstitutionOut:
    institution = assets_service.update_institution(
        session, institution_id, **payload.model_dump(exclude_unset=True)
    )
    return InstitutionOut.model_validate(assets_service.serialize_institution(institution))


@router.delete(
    "/api/institutions/{institution_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除机构"
)
def delete_institution(institution_id: int, session: Session = SessionDep) -> None:
    assets_service.delete_institution(session, institution_id)


# -----------------------------------------------------------------------------
# 卡面
# -----------------------------------------------------------------------------
@router.get("/api/card-artworks", response_model=list[CardArtworkOut], summary="卡面列表")
def list_artworks(session: Session = SessionDep) -> list[CardArtworkOut]:
    return [
        CardArtworkOut.model_validate(assets_service.serialize_artwork(item))
        for item in assets_service.list_card_artworks(session)
    ]


@router.post(
    "/api/card-artworks",
    response_model=CardArtworkOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增卡面",
)
def create_artwork(payload: CardArtworkCreate, session: Session = SessionDep) -> CardArtworkOut:
    artwork = assets_service.create_card_artwork(
        session,
        key=payload.key,
        name=payload.name,
        spec=payload.spec,
        kind=payload.kind,
        file_ref=payload.file_ref,
    )
    return CardArtworkOut.model_validate(assets_service.serialize_artwork(artwork))


@router.patch("/api/card-artworks/{artwork_id}", response_model=CardArtworkOut, summary="更新卡面")
def update_artwork(
    artwork_id: int,
    payload: CardArtworkUpdate,
    session: Session = SessionDep,
) -> CardArtworkOut:
    artwork = assets_service.update_card_artwork(
        session, artwork_id, **payload.model_dump(exclude_unset=True)
    )
    return CardArtworkOut.model_validate(assets_service.serialize_artwork(artwork))


@router.delete("/api/card-artworks/{artwork_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除卡面")
def delete_artwork(artwork_id: int, session: Session = SessionDep) -> None:
    assets_service.delete_card_artwork(session, artwork_id)
