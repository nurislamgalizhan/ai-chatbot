"""FastAPI dependencies: DB session, Redis, and the single auth gate.

require_admin is THE authorization model of this system: a valid session
grants everything, no session grants nothing but /healthz. There are no
roles and none may be added (SPEC §10).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

# NOTE: everything referenced from FastAPI dependency signatures must be
# imported at runtime, not under TYPE_CHECKING: FastAPI evaluates the
# annotations lazily and an unresolvable name silently degrades the
# parameter into a query field.
from app.config import Settings
from app.models import AdminSession, AdminUser
from app.security.sessions import authenticate_session

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Не аутентифицировано"
)


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Commit on success, roll back on any exception.

    Endpoints that must persist state even though the request fails
    (failed-login counters, auth audit records) call `await db.commit()`
    explicitly before raising.
    """
    async with request.app.state.sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def client_ip(request: Request) -> str | None:
    # The app sits behind our own gateway; X-Real-IP is set there.
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else None


async def require_admin(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AdminUser:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise UNAUTHENTICATED
    resolved = await authenticate_session(db, settings, token)
    if resolved is None:
        raise UNAUTHENTICATED
    user, session = resolved
    request.state.admin_user = user
    request.state.admin_session = session
    return user


def current_session(request: Request) -> AdminSession:
    session: AdminSession = request.state.admin_session
    return session
