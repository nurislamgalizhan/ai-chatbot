"""Two-step login: password -> short-lived challenge -> TOTP -> session.

Security properties enforced here (covered by tests):
- unknown phone and wrong password are indistinguishable in time and response;
- progressive lockout via failed_login_count / locked_until;
- the challenge state grants no API access — it is not a session;
- a TOTP code is single-use; recovery codes are single-use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models import AdminUser, LoginChallenge, TotpRecoveryCode
from app.security.audit import record_audit
from app.security.passwords import verify_password
from app.security.tokens import generate_token, hash_token
from app.security.totp import decrypt_totp_secret, verify_totp_code

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings


class LoginRejected(Exception):
    """Generic rejection — same message for every credential failure."""

    detail = "Неверный номер телефона или пароль"


class AccountLocked(Exception):
    detail = "Слишком много неудачных попыток. Попробуйте позже."


class ChallengeRejected(Exception):
    detail = "Неверный или истёкший код подтверждения"


@dataclass(frozen=True)
class PasswordAccepted:
    challenge_token: str


def _now() -> datetime:
    return datetime.now(UTC)


async def start_login(
    db: AsyncSession,
    settings: Settings,
    phone: str,
    password: str,
    ip: str | None,
    correlation_id: str | None,
) -> PasswordAccepted:
    user = (
        await db.execute(select(AdminUser).where(AdminUser.phone == phone))
    ).scalar_one_or_none()

    if user is not None and user.locked_until is not None and user.locked_until > _now():
        await record_audit(
            db,
            action="auth.login_locked",
            entity_type="admin_user",
            entity_id=str(user.id),
            ip=ip,
            correlation_id=correlation_id,
        )
        raise AccountLocked

    # verify_password runs argon2 against a dummy hash when user is None,
    # keeping timing and the response identical to a wrong password.
    password_ok = verify_password(user.password_hash if user is not None else None, password)
    if user is None or not user.is_active or not password_ok:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= settings.lockout_threshold:
                # Progressive: 15m, 30m, 60m, ... doubling per extra failure.
                exponent = user.failed_login_count - settings.lockout_threshold
                minutes = settings.lockout_base_minutes * (2**exponent)
                user.locked_until = _now() + timedelta(minutes=minutes)
        await record_audit(
            db,
            action="auth.login_failed",
            entity_type="admin_user",
            entity_id=str(user.id) if user is not None else None,
            ip=ip,
            correlation_id=correlation_id,
        )
        raise LoginRejected

    token = generate_token()
    db.add(
        LoginChallenge(
            user_id=user.id,
            token_hash=hash_token(token),
            ip=ip,
            expires_at=_now() + timedelta(seconds=settings.login_challenge_ttl_seconds),
        )
    )
    await record_audit(
        db,
        action="auth.password_accepted",
        entity_type="admin_user",
        entity_id=str(user.id),
        ip=ip,
        correlation_id=correlation_id,
    )
    await db.flush()
    return PasswordAccepted(challenge_token=token)


async def complete_login(
    db: AsyncSession,
    settings: Settings,
    redis: Redis,
    challenge_token: str,
    code: str,
    ip: str | None,
    correlation_id: str | None,
) -> AdminUser:
    challenge = (
        await db.execute(
            select(LoginChallenge).where(LoginChallenge.token_hash == hash_token(challenge_token))
        )
    ).scalar_one_or_none()
    if challenge is None or challenge.consumed_at is not None or challenge.expires_at <= _now():
        raise ChallengeRejected

    user = (
        await db.execute(select(AdminUser).where(AdminUser.id == challenge.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active or user.totp_secret_encrypted is None:
        raise ChallengeRejected

    secret = decrypt_totp_secret(settings, user.totp_secret_encrypted)
    ok = await verify_totp_code(redis, user.id, secret, code)
    if not ok:
        ok = await _try_recovery_code(db, user.id, code)
    if not ok:
        await record_audit(
            db,
            action="auth.totp_failed",
            entity_type="admin_user",
            entity_id=str(user.id),
            ip=ip,
            correlation_id=correlation_id,
        )
        raise ChallengeRejected

    challenge.consumed_at = _now()
    user.failed_login_count = 0
    user.locked_until = None
    await record_audit(
        db,
        action="auth.login_success",
        entity_type="admin_user",
        entity_id=str(user.id),
        ip=ip,
        correlation_id=correlation_id,
    )
    await db.flush()
    return user


async def _try_recovery_code(db: AsyncSession, user_id: int, code: str) -> bool:
    codes = (
        (
            await db.execute(
                select(TotpRecoveryCode).where(
                    TotpRecoveryCode.user_id == user_id,
                    TotpRecoveryCode.used_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in codes:
        if verify_password(row.code_hash, code):
            row.used_at = _now()
            await db.flush()
            return True
    return False
