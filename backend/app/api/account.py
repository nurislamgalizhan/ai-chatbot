"""Management of the single admin account: password, sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminSession, AdminUser, AuditLog, TotpRecoveryCode
from app.schemas.auth import (
    ChangePasswordRequest,
    OkResponse,
    SessionInfoResponse,
    SessionListResponse,
    SessionUserResponse,
)
from app.schemas.system_status import (
    AuditResponse,
    AuditRow,
    ConfirmPasswordRequest,
    LoginAttemptRow,
    LoginHistoryResponse,
    RecoveryCodesResponse,
    RecoveryCodesStatusResponse,
)
from app.security.audit import record_audit
from app.security.deps import client_ip, current_session, get_db, require_admin
from app.security.passwords import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.security.sessions import revoke_all_sessions, revoke_session
from app.security.totp import generate_recovery_codes

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
                    # An expired session grants nothing; listing it as
                    # "active device" tells the owner their account is
                    # open somewhere it is not, and invites a pointless
                    # "завершить" click that changes nothing.
                    AdminSession.expires_at > datetime.now(UTC),
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


# --------------------------- journal (SPEC §12.8) -------------------------

#: Owner-facing names for audit actions. An unknown action is shown as
#: its own code rather than hidden: the journal must never lie by
#: omission.
_ACTION_LABELS: dict[str, str] = {
    "auth.login_success": "Вход в панель",
    "auth.login_failed": "Неудачная попытка входа",
    "auth.login_locked": "Вход заблокирован после неудачных попыток",
    "auth.password_accepted": "Пароль принят, запрошен код",
    "auth.totp_failed": "Неверный код подтверждения",
    "account.session_revoked": "Завершена сессия",
    "account.sessions_revoked_all": "Завершены все другие сессии",
    "account.password_changed": "Изменён пароль",
    "account.recovery_codes_regenerated": "Перевыпущены коды восстановления",
    "settings.updated": "Изменены настройки",
    "limits.updated": "Изменены лимиты",
    "limits.fx_updated": "Изменён курс валюты",
    "limits.prices_updated": "Изменены цены моделей",
    "agent.instruction_created": "Создана версия инструкции",
    "agent.instruction_activated": "Активирована версия инструкции",
    "kb.document_created": "Добавлен документ базы знаний",
    "kb.document_updated": "Изменён документ базы знаний",
    "kb.document_deleted": "Удалён документ базы знаний",
    "kb.faq_created": "Добавлена запись «вопрос — ответ»",
    "kb.faq_updated": "Изменена запись «вопрос — ответ»",
    "kb.faq_deleted": "Удалена запись «вопрос — ответ»",
    "chat.mode_changed": "Изменён режим диалога",
}

_LOGIN_ACTIONS = (
    "auth.login_success",
    "auth.login_failed",
    "auth.login_locked",
    "auth.totp_failed",
)


def _label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


@router.get("/journal", response_model=AuditResponse)
async def journal(
    db: Annotated[AsyncSession, Depends(get_db)],
    action: Annotated[str, Query(max_length=64)] = "",
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditResponse:
    """What changed in the system and who did it (SPEC §12.8).

    The rows themselves never contain customer texts or secrets — that
    is the contract of `record_audit` — so the journal is safe to show
    in full.
    """
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if action:
        query = query.where(AuditLog.action.startswith(action))
    if cursor:
        query = query.where(AuditLog.id < cursor)
    rows = (await db.execute(query)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return AuditResponse(
        items=[
            AuditRow(
                id=row.id,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                ip=row.ip,
                created_at=row.created_at,
                summary=_label(row.action),
            )
            for row in page
        ],
        next_cursor=page[-1].id if has_more and page else None,
    )


@router.get("/login-history", response_model=LoginHistoryResponse)
async def login_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> LoginHistoryResponse:
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action.in_(_LOGIN_ACTIONS))
                .order_by(AuditLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    last_success = (
        await db.execute(
            select(func.max(AuditLog.created_at)).where(AuditLog.action == "auth.login_success")
        )
    ).scalar()
    return LoginHistoryResponse(
        items=[
            LoginAttemptRow(
                id=row.id,
                created_at=row.created_at,
                ip=row.ip,
                user_agent=None,
                outcome=_label(row.action),
            )
            for row in rows
        ],
        last_success_at=last_success,
    )


@router.post("/recovery-codes", response_model=RecoveryCodesResponse)
async def regenerate_recovery_codes(
    body: ConfirmPasswordRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecoveryCodesResponse:
    """Issue a fresh set and invalidate the old one.

    The current password is required: recovery codes bypass the second
    factor, so minting them from an unattended open session would hand
    someone a permanent way back in. Shown exactly once — only argon2
    hashes are stored, so nobody, including us, can print them again.
    """
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Текущий пароль неверен")
    await db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
    codes = generate_recovery_codes()
    for code in codes:
        db.add(TotpRecoveryCode(user_id=user.id, code_hash=hash_password(code)))
    await record_audit(
        db,
        action="account.recovery_codes_regenerated",
        actor_user_id=user.id,
        entity_type="admin_user",
        entity_id=str(user.id),
        after={"count": len(codes)},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return RecoveryCodesResponse(codes=codes)


@router.get("/recovery-codes/status", response_model=RecoveryCodesStatusResponse)
async def recovery_codes_status(
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecoveryCodesStatusResponse:
    unused = (
        await db.execute(
            select(func.count())
            .select_from(TotpRecoveryCode)
            .where(TotpRecoveryCode.user_id == user.id, TotpRecoveryCode.used_at.is_(None))
        )
    ).scalar_one()
    total = (
        await db.execute(
            select(func.count())
            .select_from(TotpRecoveryCode)
            .where(TotpRecoveryCode.user_id == user.id)
        )
    ).scalar_one()
    return RecoveryCodesStatusResponse(unused=int(unused), total=int(total))
