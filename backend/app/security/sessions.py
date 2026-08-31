"""Server-side session lifecycle. Cookies carry opaque tokens; the DB stores
only SHA-256 hashes, so a DB leak does not leak usable sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.models import AdminSession, AdminUser
from app.security.tokens import generate_token, hash_token

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings

# last_seen_at is persisted at most this often to avoid a write per request.
_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    session_id: int


def _now() -> datetime:
    return datetime.now(UTC)


async def create_session(
    db: AsyncSession,
    settings: Settings,
    user: AdminUser,
    user_agent: str | None,
    ip: str | None,
) -> NewSession:
    token = generate_token()
    csrf_token = generate_token()
    session = AdminSession(
        user_id=user.id,
        session_token_hash=hash_token(token),
        user_agent=(user_agent or "")[:300] or None,
        ip=ip,
        expires_at=_now() + timedelta(hours=settings.session_absolute_hours),
    )
    db.add(session)
    await db.flush()
    return NewSession(token=token, csrf_token=csrf_token, session_id=session.id)


async def authenticate_session(
    db: AsyncSession, settings: Settings, token: str
) -> tuple[AdminUser, AdminSession] | None:
    """Resolve a cookie token to an active user, enforcing idle and absolute
    timeouts. Any failure mode returns None (uniform 401 upstream)."""
    result = await db.execute(
        select(AdminUser, AdminSession)
        .join(AdminSession, AdminSession.user_id == AdminUser.id)
        .where(AdminSession.session_token_hash == hash_token(token))
    )
    row = result.first()
    if row is None:
        return None
    user, session = row
    now = _now()
    if (
        session.revoked_at is not None
        or session.expires_at <= now
        or session.last_seen_at + timedelta(minutes=settings.session_idle_minutes) <= now
        or not user.is_active
    ):
        return None
    if now - session.last_seen_at >= _LAST_SEEN_WRITE_INTERVAL:
        session.last_seen_at = now
        await db.flush()
    return user, session


async def revoke_session(db: AsyncSession, session: AdminSession) -> None:
    if session.revoked_at is None:
        session.revoked_at = _now()
        await db.flush()


async def revoke_all_sessions(
    db: AsyncSession, user_id: int, except_session_id: int | None = None
) -> int:
    stmt = (
        update(AdminSession)
        .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    if except_session_id is not None:
        stmt = stmt.where(AdminSession.id != except_session_id)
    result = await db.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
