"""元数据路由 —— 把领域枚举与字典下发给前端。

前端据此生成下拉选项，而不是自己手抄一份取值列表：
手抄的那份一定会与后端不同步，而症状是"某个新类型在界面上选不到"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.domain import (
    AccountType,
    CategoryKind,
    ProjectStatus,
    TransactionSource,
    TransactionStatus,
    TransactionType,
)
from ...db.models import Currency
from ..deps import get_session
from ..schemas import CurrencyOut, EnumsOut

__all__ = ["router"]

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/enums", response_model=EnumsOut, summary="领域枚举取值")
def get_enums() -> EnumsOut:
    """返回全部枚举取值。

    取值来自 ``core.domain``（与数据库里存的是同一份来源），
    因此新增一种账户类型时，前端会自动获得它，无需改前端代码。
    """
    return EnumsOut(
        account_types=[item.value for item in AccountType],
        category_kinds=[item.value for item in CategoryKind],
        transaction_types=[item.value for item in TransactionType],
        transaction_statuses=[item.value for item in TransactionStatus],
        transaction_sources=[item.value for item in TransactionSource],
        directions=["in", "out"],
        project_statuses=[item.value for item in ProjectStatus],
    )


@router.get("/currencies", response_model=list[CurrencyOut], summary="币种字典")
def list_currencies(
    request: Request,
    session: Session = Depends(get_session),
) -> list[Currency]:
    """返回启用的币种（含最小单位位数 —— 前端格式化金额时需要它）。"""
    del request  # 仅用于保持依赖签名一致；会话已经由 get_session 提供
    return list(
        session.scalars(
            select(Currency).where(Currency.is_active.is_(True)).order_by(Currency.sort_order, Currency.code)
        ).all()
    )
