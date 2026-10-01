"""回收站与批量操作路由（P1 尾巴 T4 / T1）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...services import transactions as transactions_service
from ...services import trash as trash_service
from ...services.transactions import UNSET
from ..deps import get_session
from ..schemas import BatchDeleteRequest, BatchUpdateRequest, TrashItemOut, TrashListOut

__all__ = ["router"]

router = APIRouter(tags=["trash"])

SessionDep = Depends(get_session)


# -----------------------------------------------------------------------------
# 回收站
# -----------------------------------------------------------------------------
@router.get("/api/trash", summary="回收站概览（每个实体各有多少条）")
def trash_summary(session: Session = SessionDep) -> dict[str, Any]:
    """一次返回全部实体的计数。

    界面要显示"流水 3 · 账户 1"这样的概览，让前端逐个实体去问会明显变慢。
    """
    items = trash_service.summary(session)
    return {"entities": items, "total": sum(item["count"] for item in items)}


@router.get("/api/trash/{entity}", response_model=TrashListOut, summary="回收站列表")
def trash_list(
    entity: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = SessionDep,
) -> Any:
    return trash_service.list_trash(session, entity, limit=limit, offset=offset)


@router.post(
    "/api/trash/{entity}/{row_id}/restore",
    response_model=TrashItemOut,
    summary="从回收站恢复",
)
def trash_restore(entity: str, row_id: int, session: Session = SessionDep) -> Any:
    """恢复一条。

    走各领域服务自己的 restore：恢复流水要重算日结、恢复账户会改变整条
    净值曲线，直接改 `deleted_at` 会让那些缓存停在旧值上。
    """
    return trash_service.restore(session, entity, row_id)


@router.delete(
    "/api/trash/{entity}/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="彻底删除（不可恢复）",
)
def trash_purge(entity: str, row_id: int, session: Session = SessionDep) -> None:
    """彻底删除。

    有外键引用时返回 409 并说明被什么挡住 —— 刻意不做级联删除：
    级联删用户数据不可逆，不该由一个"清理回收站"的按钮触发。
    """
    trash_service.purge(session, entity, row_id)


# -----------------------------------------------------------------------------
# 批量编辑（挂在流水下，语义上属于流水）
# -----------------------------------------------------------------------------
@router.post("/api/transactions/batch", summary="批量修改流水")
def batch_update(payload: BatchUpdateRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    """批量修改。

    只应用**显式给出**的字段（用 ``model_fields_set`` 判断），
    因此"没传 category_id"与"传了 category_id=null"是两件事 ——
    前者不动分类，后者把分类清掉。少了这个区分，
    批量操作要么改不动、要么会清掉用户没打算动的字段。
    """
    provided = payload.model_fields_set

    def value_or_unset(name: str) -> Any:
        return getattr(payload, name) if name in provided else UNSET

    return transactions_service.batch_update(
        session,
        payload.ids,
        category_id=value_or_unset("category_id"),
        project_id=value_or_unset("project_id"),
        member_id=value_or_unset("member_id"),
        status=value_or_unset("status"),
        tag_ids=payload.tag_ids if "tag_ids" in provided else None,
        add_tag_ids=payload.add_tag_ids,
    )


@router.post("/api/transactions/batch-delete", summary="批量删除流水（可恢复）")
def batch_delete(payload: BatchDeleteRequest, session: Session = SessionDep) -> dict[str, Any]:
    return transactions_service.batch_delete(session, payload.ids)
