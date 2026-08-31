from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser, Tag
from app.schemas.chats import TagCreateRequest, TagListResponse, TagResponse
from app.security.audit import record_audit
from app.security.deps import client_ip, get_db, require_admin

router = APIRouter(prefix="/api/tags", tags=["tags"], dependencies=[Depends(require_admin)])


@router.get("", response_model=TagListResponse)
async def list_tags(db: Annotated[AsyncSession, Depends(get_db)]) -> TagListResponse:
    rows = (await db.execute(select(Tag).order_by(Tag.name))).scalars().all()
    return TagListResponse(items=[TagResponse(id=t.id, name=t.name, color=t.color) for t in rows])


@router.post("", response_model=TagResponse)
async def create_tag(
    body: TagCreateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TagResponse:
    name = body.name.strip()
    duplicate = (
        await db.execute(select(Tag.id).where(func.lower(Tag.name) == name.lower()))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Такой тег уже существует")
    tag = Tag(name=name, color=body.color)
    db.add(tag)
    await db.flush()
    await record_audit(
        db,
        action="tag.created",
        actor_user_id=user.id,
        entity_type="tag",
        entity_id=str(tag.id),
        after={"name": name},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return TagResponse(id=tag.id, name=tag.name, color=tag.color)
