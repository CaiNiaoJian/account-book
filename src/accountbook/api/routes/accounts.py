"""账户路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ...services import accounts as accounts_service
from ..deps import get_session
from ..schemas import AccountCreate, AccountOut, AccountOverviewOut, AccountUpdate

__all__ = ["router"]

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

SessionDep = Depends(get_session)


@router.get("", response_model=list[AccountOut], summary="账户列表")
def list_accounts(
    include_archived: bool = Query(default=False),
    include_deleted: bool = Query(default=False),
    session: Session = SessionDep,
) -> list[AccountOut]:
    rows = accounts_service.list_accounts(
        session, include_archived=include_archived, include_deleted=include_deleted
    )
    return [AccountOut.model_validate(row) for row in rows]


@router.get("/overview", response_model=AccountOverviewOut, summary="资产总览（含余额）")
def overview(
    include_archived: bool = Query(default=False),
    session: Session = SessionDep,
) -> AccountOverviewOut:
    """账户明细 + 资产/负债/净值汇总。

    注意路由顺序：``/overview`` 必须定义在 ``/{account_id}`` 之前，
    否则 FastAPI 会把 "overview" 当成账户 id 去解析。
    """
    return AccountOverviewOut.model_validate(
        accounts_service.overview(session, include_archived=include_archived)
    )


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED, summary="创建账户")
def create_account(payload: AccountCreate, session: Session = SessionDep) -> AccountOut:
    account = accounts_service.create_account(session, **payload.model_dump())
    return AccountOut.model_validate(account)


@router.get("/{account_id}", response_model=AccountOut, summary="账户详情")
def get_account(account_id: int, session: Session = SessionDep) -> AccountOut:
    return AccountOut.model_validate(accounts_service.get_account(session, account_id))


@router.patch("/{account_id}", response_model=AccountOut, summary="更新账户")
def update_account(
    account_id: int,
    payload: AccountUpdate,
    session: Session = SessionDep,
) -> AccountOut:
    changes = payload.model_dump(exclude_unset=True)
    return AccountOut.model_validate(accounts_service.update_account(session, account_id, **changes))


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除账户（软删除）")
def delete_account(account_id: int, session: Session = SessionDep) -> None:
    accounts_service.delete_account(session, account_id)


@router.post("/{account_id}/restore", response_model=AccountOut, summary="从回收站恢复账户")
def restore_account(account_id: int, session: Session = SessionDep) -> AccountOut:
    return AccountOut.model_validate(accounts_service.restore_account(session, account_id))


@router.get("/{account_id}/balance", summary="账户余额")
def get_balance(account_id: int, session: Session = SessionDep) -> dict[str, int]:
    return {"account_id": account_id, "balance_minor": accounts_service.account_balance(session, account_id)}
