from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    OkResponse,
    VerifyTotpRequest,
)
from app.security import rate_limit
from app.security.audit import record_audit
from app.security.deps import (
    client_ip,
    current_session,
    get_app_settings,
    get_db,
    get_redis,
    require_admin,
)
from app.security.sessions import create_session, revoke_session
from app.security.tokens import hash_token
from app.services import auth_flow
from app.services.phone import PhoneValidationError, normalize_phone

router = APIRouter(prefix="/api/auth", tags=["auth"])

_RATE_WINDOW_SECONDS = 900
_TOO_MANY = HTTPException(
    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    detail="Слишком много попыток. Попробуйте позже.",
)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> LoginResponse:
    ip = client_ip(request)
    try:
        phone = normalize_phone(body.phone)
    except PhoneValidationError:
        # Same response as wrong credentials: no phone-format oracle.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=auth_flow.LoginRejected.detail,
        ) from None

    ip_allowed = await rate_limit.allow(
        redis, f"rl:login:ip:{ip}", settings.login_max_attempts_ip_15m, _RATE_WINDOW_SECONDS
    )
    phone_allowed = await rate_limit.allow(
        redis,
        f"rl:login:phone:{hash_token(phone)}",
        settings.login_max_attempts_phone_15m,
        _RATE_WINDOW_SECONDS,
    )
    if not (ip_allowed and phone_allowed):
        raise _TOO_MANY

    try:
        accepted = await auth_flow.start_login(
            db, settings, phone, body.password, ip, _correlation_id(request)
        )
    except auth_flow.AccountLocked as exc:
        # Same status and body as a wrong password: a 429 only for the
        # real account would tell an attacker which phone is the admin's.
        await db.commit()  # persist the audit record
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=auth_flow.LoginRejected.detail
        ) from exc
    except auth_flow.LoginRejected as exc:
        await db.commit()  # persist failed_login_count / lockout / audit
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc
    return LoginResponse(challenge_token=accepted.challenge_token)


@router.post("/verify-totp", response_model=OkResponse)
async def verify_totp(
    body: VerifyTotpRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> OkResponse:
    ip = client_ip(request)
    allowed = await rate_limit.allow(
        redis, f"rl:totp:ip:{ip}", settings.login_max_attempts_ip_15m, _RATE_WINDOW_SECONDS
    )
    if not allowed:
        raise _TOO_MANY

    try:
        user = await auth_flow.complete_login(
            db, settings, redis, body.challenge_token, body.code, ip, _correlation_id(request)
        )
    except auth_flow.ChallengeRejected as exc:
        await db.commit()  # persist the totp_failed audit record
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc

    new_session = await create_session(db, settings, user, request.headers.get("User-Agent"), ip)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=new_session.token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/api",
        max_age=settings.session_absolute_hours * 3600,
    )
    # Double-submit CSRF cookie: readable by the frontend, echoed back in
    # the X-CSRF-Token header on every mutating request.
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=new_session.csrf_token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_absolute_hours * 3600,
    )
    return OkResponse()


@router.post("/logout", response_model=OkResponse, dependencies=[Depends(require_admin)])
async def logout(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> OkResponse:
    session = current_session(request)
    await revoke_session(db, session)
    await record_audit(
        db,
        action="auth.logout",
        actor_user_id=session.user_id,
        entity_type="admin_session",
        entity_id=str(session.id),
        ip=client_ip(request),
        correlation_id=_correlation_id(request),
    )
    response.delete_cookie(settings.session_cookie_name, path="/api")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return OkResponse()
