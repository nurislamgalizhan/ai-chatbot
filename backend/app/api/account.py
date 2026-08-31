"""Management of the single admin account: password, sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminSession, AdminUser
from app.schemas.auth import (
    ChangePasswordRequest,
    OkResponse,
    SessionInfoResponse,
    SessionListResponse,
    SessionUserResponse,
)
from app.security.audit import record_audit
from app.security.deps import client_ip, current_session, get_db, require_admin
from app.security.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.security.sessions import revoke_all_sessions, revoke_session

router = APIRouter(prefix="/api/account", tags=["account"], dependencies=[Depends(require_admin)])


@router.get("", response_model=SessionUserResponse)
async def get_account(
    user: Annotated[AdminUser, Depends(require_admin)],
) -> SessionUserResponse:
    return SessionUserResponse(
        phone=user.phone,
        full_name=user.full_name,
        password_changed_at=user.password_changed_at,
    )


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkResponse:
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Текущий пароль неверен")
    error = validate_password_strength(body.new_password)
    if error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error)

    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    session = current_session(request)
    # Changing the password revokes every OTHER session (SPEC §10).
    revoked = await revoke_all_sessions(db, user.id, except_session_id=session.id)
    await record_audit(
        db,
        action="account.password_changed",
        actor_user_id=user.id,
        entity_type="admin_user",
        entity_id=str(user.id),
        after={"other_sessions_revoked": revoked},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return OkResponse()


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionListResponse:
    current = current_session(request)
    rows = (
        (
            await db.execute(
                select(AdminSession)
                .where(
                    AdminSession.user_id == user.id,
                    AdminSession.revoked_at.is_(None),
                )
                .order_by(AdminSession.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return SessionListResponse(
        sessions=[
            SessionInfoResponse(
                id=row.id,
                user_agent=row.user_agent,
                ip=row.ip,
                created_at=row.created_at,
                last_seen_at=row.last_seen_at,
                is_current=row.id == current.id,
            )
            for row in rows
        ]
    )


@router.post("/sessions/{session_id}/revoke", response_model=OkResponse)
async def revoke_one_session(
    session_id: int,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkResponse:
    row = (
        await db.execute(
            select(AdminSession).where(
                AdminSession.id == session_id,
                AdminSession.user_id == user.id,
                AdminSession.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Nonexistent or already-revoked id -> 404, not a silent no-op.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сессия не найдена")
    await revoke_session(db, row)
    await record_audit(
        db,
        action="account.session_revoked",
        actor_user_id=user.id,
        entity_type="admin_session",
        entity_id=str(row.id),
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return OkResponse()


@router.post("/sessions/revoke-others", response_model=OkResponse)
async def revoke_other_sessions(
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkResponse:
    current = current_session(request)
    revoked = await revoke_all_sessions(db, user.id, except_session_id=current.id)
    await record_audit(
        db,
        action="account.sessions_revoked_all",
        actor_user_id=user.id,
        entity_type="admin_user",
        entity_id=str(user.id),
        after={"revoked": revoked},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return OkResponse()
