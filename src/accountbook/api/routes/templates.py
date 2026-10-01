"""记账模板与剪贴板解析路由（P1 收尾）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ...services import templates as templates_service
from ..deps import get_session
from ..schemas import (
    ParseTextRequest,
    ParseTextResult,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/templates", tags=["templates"])

SessionDep = Depends(get_session)


@router.get("", response_model=list[TemplateOut], summary="模板列表（按最常用排序）")
def list_templates(session: Session = SessionDep) -> list[TemplateOut]:
    return [
        TemplateOut.model_validate(templates_service.serialize(item))
        for item in templates_service.list_templates(session)
    ]


@router.post("", response_model=TemplateOut, status_code=status.HTTP_201_CREATED, summary="新建模板")
def create_template(payload: TemplateCreate, session: Session = SessionDep) -> TemplateOut:
    template = templates_service.create_template(session, **payload.model_dump())
    return TemplateOut.model_validate(templates_service.serialize(template))


@router.patch("/{template_id}", response_model=TemplateOut, summary="更新模板")
def update_template(template_id: int, payload: TemplateUpdate, session: Session = SessionDep) -> TemplateOut:
    template = templates_service.update_template(
        session, template_id, **payload.model_dump(exclude_unset=True)
    )
    return TemplateOut.model_validate(templates_service.serialize(template))


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除模板")
def delete_template(template_id: int, session: Session = SessionDep) -> None:
    templates_service.delete_template(session, template_id)


@router.post("/{template_id}/apply", response_model=TemplateOut, summary="套用模板（记一次使用）")
def apply_template(template_id: int, session: Session = SessionDep) -> TemplateOut:
    """套用模板返回草稿并累加使用次数。

    刻意**不直接创建流水**：模板的金额可能是空的（"只填结构"），
    而且用户几乎总要先看一眼再保存。
    """
    return TemplateOut.model_validate(templates_service.apply_template(session, template_id))


@router.post("/parse", response_model=ParseTextResult, summary="解析一段文本成记账草稿")
def parse_text(payload: ParseTextRequest, session: Session = SessionDep) -> dict[str, Any]:
    """把剪贴板/随手输入解析成草稿。

    返回 ``unmatched``（认不出的词）是接口契约的一部分：
    界面**必须**把它显示出来，用户才知道哪些内容没被采纳。
    静默丢弃会让人以为都识别到了。
    """
    return templates_service.parse_quick_text(session, payload.text)
