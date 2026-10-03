"""流水路由 —— 记账核心的对外接口。"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...core.errors import ConflictError, ValidationError
from ...db.models import TransactionRequestKey
from ...services import transactions as transactions_service
from ...services.stats import transaction_brief
from ..deps import get_session
from ..schemas import TransactionCreate, TransactionListOut, TransactionOut, TransactionUpdate

__all__ = ["router"]

router = APIRouter(prefix="/api/transactions", tags=["transactions"])

SessionDep = Depends(get_session)


def _to_out(transaction: object) -> TransactionOut:
    """ORM 流水 → 响应模型。

    复用 ``stats.transaction_brief``：首页"最近记录"与流水列表必须输出
    完全相同的字段与口径，否则会出现"同一个东西两处显示不一样"。
    """
    return TransactionOut.model_validate(transaction_brief(transaction))  # type: ignore[arg-type]


def _expand(values: list[str] | None) -> list[str]:
    """展开多值查询参数，同时支持**重复参数**与**逗号分隔**。

    FastAPI 默认只认重复参数（``?types=a&types=b``），但逗号分隔
    （``?types=a,b``）是更常见的书写习惯 —— 手工调接口、写脚本、
    从浏览器地址栏试接口时几乎都会写成后者，而它会被当成单个值
    ``"a,b"``，结果是"筛什么都没结果"却没有任何报错。
    两种写法都接受，成本极低，能省掉一整类困惑。
    """
    expanded: list[str] = []
    for value in values or []:
        expanded.extend(part.strip() for part in str(value).split(",") if part.strip())
    return expanded


def _expand_ints(values: list[int] | None) -> list[int]:
    """整数多值参数同样支持逗号分隔（例如 ``?account_ids=1,2``）。"""
    result: list[int] = []
    for part in _expand([str(value) for value in values or []]):
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValidationError(f"不是合法的整数 id：{part}", field="ids") from exc
    return result


@router.get("", response_model=TransactionListOut, summary="流水列表（筛选 / 搜索 / 分页）")
def list_transactions(
    session: Session = SessionDep,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    account_ids: list[int] | None = Query(default=None),
    category_ids: list[int] | None = Query(default=None),
    tag_ids: list[int] | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    project_id: int | None = Query(default=None),
    member_id: int | None = Query(default=None),
    min_amount_minor: int | None = Query(default=None),
    max_amount_minor: int | None = Query(default=None),
    keyword: str = Query(default="", description="在商户与备注中搜索"),
    include_transfers: bool = Query(default=True),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> TransactionListOut:
    query = transactions_service.TransactionQuery(
        start=start,
        end=end,
        account_ids=_expand_ints(account_ids),
        category_ids=_expand_ints(category_ids),
        tag_ids=_expand_ints(tag_ids),
        types=_expand(types),
        statuses=_expand(statuses),
        project_id=project_id,
        member_id=member_id,
        min_amount_minor=min_amount_minor,
        max_amount_minor=max_amount_minor,
        keyword=keyword,
        include_transfers=include_transfers,
        order=order,
        limit=limit,
        offset=offset,
    )
    items, total = transactions_service.list_transactions(session, query)
    return TransactionListOut(
        items=[_to_out(item) for item in items], total=total, limit=limit, offset=offset
    )


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED, summary="记一笔")
def create_transaction(payload: TransactionCreate, request: Request, session: Session = SessionDep) -> TransactionOut:
    key = request.headers.get('Idempotency-Key', '')
    digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    if key:
        if len(key) > 128 or not key.isascii() or not all(c.isalnum() or c in '-_' for c in key):
            raise ValidationError('记账请求标识不合法')
        # Serialize only keyed creates, including the first lookup, to protect simultaneous retries.
        session.execute(text('BEGIN IMMEDIATE'))
        previous = session.get(TransactionRequestKey, key)
        if previous:
            if previous.payload_hash != digest:
                raise ConflictError('同一次请求的内容发生变化，请重新提交')
            return _to_out(transactions_service.get_transaction(session, previous.transaction_id))
    data = payload.model_dump(exclude_none=True)
    # 分账与标签单独取出：它们的语义是"整体替换"，而不是普通字段赋值
    splits = data.pop("splits", None)
    tag_ids = data.pop("tag_ids", None)
    transaction = transactions_service.create_transaction(
        session,
        splits=[dict(item) for item in splits] if splits else None,
        tag_ids=tag_ids,
        **data,
    )
    if key:
        session.add(TransactionRequestKey(key=key, payload_hash=digest, transaction_id=transaction.id))
    return _to_out(transaction)


@router.get("/{transaction_id}", response_model=TransactionOut, summary="流水详情")
def get_transaction(transaction_id: int, session: Session = SessionDep) -> TransactionOut:
    return _to_out(transactions_service.get_transaction(session, transaction_id))


@router.patch("/{transaction_id}", response_model=TransactionOut, summary="更新流水")
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    session: Session = SessionDep,
) -> TransactionOut:
    changes = payload.model_dump(exclude_unset=True)
    splits = changes.pop("splits", None)
    tag_ids = changes.pop("tag_ids", None)
    transaction = transactions_service.update_transaction(
        session,
        transaction_id,
        splits=[dict(item) for item in splits] if splits is not None else None,
        tag_ids=tag_ids,
        **changes,
    )
    return _to_out(transaction)


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除流水（软删除）")
def delete_transaction(transaction_id: int, session: Session = SessionDep) -> None:
    transactions_service.delete_transaction(session, transaction_id)


@router.post("/{transaction_id}/restore", response_model=TransactionOut, summary="从回收站恢复")
def restore_transaction(transaction_id: int, session: Session = SessionDep) -> TransactionOut:
    return _to_out(transactions_service.restore_transaction(session, transaction_id))
