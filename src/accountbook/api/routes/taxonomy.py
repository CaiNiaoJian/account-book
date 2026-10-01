"""标签 / 项目 / 成员路由。

为什么不用"工厂批量生成端点"
----------------------------
试过用一个 ``_register(...)`` 工厂注册三套端点，但 FastAPI 在**装饰时**就读取
函数签名来构建校验与 OpenAPI 文档，运行时再补注解为时已晚 ——
结果是请求体退化成 ``Any``，字段长度、必填等校验全部失效。

在"少写 60 行"和"校验真的生效"之间，后者没有讨论余地。
因此这里显式写三套端点：重复的是结构，受益的是**校验与文档都能工作**。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ...services import taxonomy as taxonomy_service
from ..deps import get_session
from ..schemas import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TagCreate,
    TagOut,
    TagUpdate,
)

__all__ = ["router"]

router = APIRouter(tags=["taxonomy"])

SessionDep = Depends(get_session)


# -----------------------------------------------------------------------------
# 标签
# -----------------------------------------------------------------------------
@router.get("/api/tags", response_model=list[TagOut], summary="标签列表")
def list_tags(session: Session = SessionDep) -> list[TagOut]:
    return [TagOut.model_validate(row) for row in taxonomy_service.list_tags(session)]


@router.post("/api/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED, summary="创建标签")
def create_tag(payload: TagCreate, session: Session = SessionDep) -> TagOut:
    return TagOut.model_validate(taxonomy_service.create_tag(session, **payload.model_dump()))


@router.patch("/api/tags/{tag_id}", response_model=TagOut, summary="更新标签")
def update_tag(tag_id: int, payload: TagUpdate, session: Session = SessionDep) -> TagOut:
    return TagOut.model_validate(
        taxonomy_service.update_tag(session, tag_id, **payload.model_dump(exclude_unset=True))
    )


@router.delete("/api/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除标签")
def delete_tag(tag_id: int, session: Session = SessionDep) -> None:
    taxonomy_service.delete_tag(session, tag_id)


# -----------------------------------------------------------------------------
# 项目
# -----------------------------------------------------------------------------
@router.get("/api/projects", response_model=list[ProjectOut], summary="项目列表")
def list_projects(session: Session = SessionDep) -> list[ProjectOut]:
    return [ProjectOut.model_validate(row) for row in taxonomy_service.list_projects(session)]


@router.post(
    "/api/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED, summary="创建项目"
)
def create_project(payload: ProjectCreate, session: Session = SessionDep) -> ProjectOut:
    return ProjectOut.model_validate(taxonomy_service.create_project(session, **payload.model_dump()))


@router.patch("/api/projects/{project_id}", response_model=ProjectOut, summary="更新项目")
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    session: Session = SessionDep,
) -> ProjectOut:
    return ProjectOut.model_validate(
        taxonomy_service.update_project(session, project_id, **payload.model_dump(exclude_unset=True))
    )


@router.delete("/api/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除项目")
def delete_project(project_id: int, session: Session = SessionDep) -> None:
    taxonomy_service.delete_project(session, project_id)


# -----------------------------------------------------------------------------
# 成员
# -----------------------------------------------------------------------------
@router.get("/api/members", response_model=list[MemberOut], summary="成员列表")
def list_members(session: Session = SessionDep) -> list[MemberOut]:
    return [MemberOut.model_validate(row) for row in taxonomy_service.list_members(session)]


@router.post(
    "/api/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED, summary="创建成员"
)
def create_member(payload: MemberCreate, session: Session = SessionDep) -> MemberOut:
    return MemberOut.model_validate(taxonomy_service.create_member(session, **payload.model_dump()))


@router.patch("/api/members/{member_id}", response_model=MemberOut, summary="更新成员")
def update_member(
    member_id: int,
    payload: MemberUpdate,
    session: Session = SessionDep,
) -> MemberOut:
    return MemberOut.model_validate(
        taxonomy_service.update_member(session, member_id, **payload.model_dump(exclude_unset=True))
    )


@router.delete("/api/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除成员")
def delete_member(member_id: int, session: Session = SessionDep) -> None:
    taxonomy_service.delete_member(session, member_id)
