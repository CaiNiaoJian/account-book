"""分类路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...services import categories as categories_service
from ..deps import get_session
from ..schemas import (
    CategoryCreate,
    CategoryMergeResult,
    CategoryMoveRequest,
    CategoryNode,
    CategoryOut,
    CategoryUpdate,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/categories", tags=["categories"])

SessionDep = Depends(get_session)


@router.get("", response_model=list[CategoryOut], summary="分类平铺列表")
def list_categories(
    kind: str | None = Query(default=None, description="expense / income"),
    include_hidden: bool = Query(default=True),
    session: Session = SessionDep,
) -> list[CategoryOut]:
    rows = categories_service.list_categories(session, kind=kind, include_hidden=include_hidden)
    return [CategoryOut.model_validate(row) for row in rows]


@router.get("/tree", response_model=list[CategoryNode], summary="分类树")
def category_tree(
    kind: str | None = Query(default=None, description="expense / income；留空返回全部"),
    include_hidden: bool = Query(default=True),
    session: Session = SessionDep,
) -> list[dict]:
    """嵌套树。

    一次返回整棵树（含隐藏分类）：前端需要在"管理分类"页显示隐藏项，
    而记账时的选择器自行过滤 —— 让前端决定怎么用，比后端再开一个接口更简单。
    """
    return categories_service.tree(session, kind=kind, include_hidden=include_hidden)


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED, summary="创建分类")
def create_category(payload: CategoryCreate, session: Session = SessionDep) -> CategoryOut:
    category = categories_service.create_category(session, **payload.model_dump())
    return CategoryOut.model_validate(category)


@router.get("/{category_id}", response_model=CategoryOut, summary="分类详情")
def get_category(category_id: int, session: Session = SessionDep) -> CategoryOut:
    return CategoryOut.model_validate(categories_service.get_category(session, category_id))


@router.patch("/{category_id}", response_model=CategoryOut, summary="更新分类")
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    session: Session = SessionDep,
) -> CategoryOut:
    changes = payload.model_dump(exclude_unset=True)
    return CategoryOut.model_validate(categories_service.update_category(session, category_id, **changes))


@router.post("/{category_id}/move", response_model=CategoryOut, summary="移动分类（含子树）")
def move_category(
    category_id: int,
    payload: CategoryMoveRequest,
    session: Session = SessionDep,
) -> CategoryOut:
    return CategoryOut.model_validate(
        categories_service.move_category(session, category_id, payload.parent_id)
    )


@router.post("/{category_id}/merge", response_model=CategoryMergeResult, summary="合并分类")
def merge_category(
    category_id: int,
    target_id: int = Query(description="合并目标分类 id"),
    session: Session = SessionDep,
) -> CategoryMergeResult:
    """把 ``category_id`` 合并进 ``target_id``：迁移流水与分账后软删除源分类。"""
    result = categories_service.merge_category(session, category_id, target_id)
    return CategoryMergeResult.model_validate(result)


@router.post("/{category_id}/hide", response_model=CategoryOut, summary="隐藏分类")
def hide_category(
    category_id: int,
    hidden: bool = Query(default=True),
    session: Session = SessionDep,
) -> CategoryOut:
    """内置分类不允许删除，但允许隐藏 —— 这是"不可删"的替代动作。"""
    return CategoryOut.model_validate(categories_service.set_hidden(session, category_id, hidden))


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除分类")
def delete_category(category_id: int, session: Session = SessionDep) -> None:
    categories_service.delete_category(session, category_id)
