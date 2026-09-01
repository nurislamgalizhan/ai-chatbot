"""Global system controls (authenticated). Phase 4: the emergency AI switch
— one action mutes AI in every chat without touching per-chat modes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser
from app.security.deps import get_db, require_admin
from app.services.app_settings import EMERGENCY_AI_OFF, get_setting, set_setting

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_admin)])


class AiSwitchResponse(BaseModel):
    # enabled=True means AI is allowed to work (the emergency switch is off).
    enabled: bool


class AiSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get("/ai-switch", response_model=AiSwitchResponse)
async def get_ai_switch(db: Annotated[AsyncSession, Depends(get_db)]) -> AiSwitchResponse:
    emergency_off = bool(await get_setting(db, EMERGENCY_AI_OFF))
    return AiSwitchResponse(enabled=not emergency_off)


@router.put("/ai-switch", response_model=AiSwitchResponse)
async def put_ai_switch(
    body: AiSwitchRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AiSwitchResponse:
    await set_setting(
        db,
        EMERGENCY_AI_OFF,
        not body.enabled,
        actor_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return AiSwitchResponse(enabled=body.enabled)
