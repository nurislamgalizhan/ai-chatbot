"""Two-step login: password -> short-lived challenge -> TOTP -> session.

Security properties enforced here (covered by tests):
- unknown phone and wrong password are indistinguishable in time and response;
- progressive lockout via failed_login_count / locked_until;
- the challenge state grants no API access — it is not a session;
- a TOTP code is single-use; recovery codes are single-use.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models import AdminUser, LoginChallenge, TotpRecoveryCode
from app.security import rate_limit
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
    # keeping timing and the response identical to a wrong password. It is
    # CPU-bound (~100 ms by design) and runs off the event loop.
    password_ok = await asyncio.to_thread(
        verify_password, user.password_hash if user is not None else None, password
    )
    if user is None or not user.is_active or not password_ok:
        if user is not None:
            _register_failure(user, settings)
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


def _register_failure(user: AdminUser, settings: Settings) -> None:
    """One more wrong password OR wrong second factor. Progressive
    lockout: 15m, 30m, 60m, ... doubling per extra failure."""
    user.failed_login_count += 1
    if user.failed_login_count >= settings.lockout_threshold:
        exponent = user.failed_login_count - settings.lockout_threshold
        minutes = settings.lockout_base_minutes * (2**exponent)
        user.locked_until = _now() + timedelta(minutes=minutes)


#: Wrong codes one challenge may absorb before it is burnt. A six-digit
#: code has a million values; without this an attacker holding the
#: password could keep guessing against one 300-second challenge.
_TOTP_ATTEMPTS_PER_CHALLENGE = 5


async def complete_login(
    db: AsyncSession,
    settings: Settings,
    redis: Redis,
    challenge_token: str,
    code: str,
    ip: str | None,
    correlation_id: str | None,
) -> AdminUser:
    token_hash = hash_token(challenge_token)
    challenge = (
        await db.execute(select(LoginChallenge).where(LoginChallenge.token_hash == token_hash))
    ).scalar_one_or_none()
    if challenge is None or challenge.consumed_at is not None or challenge.expires_at <= _now():
        raise ChallengeRejected

    user = (
        await db.execute(select(AdminUser).where(AdminUser.id == challenge.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active or user.totp_secret_encrypted is None:
        raise ChallengeRejected
    if user.locked_until is not None and user.locked_until > _now():
        # The password step locks the account; the second factor must
        # not be a side door around that lock.
        raise ChallengeRejected

    secret = decrypt_totp_secret(settings, user.totp_secret_encrypted)
    ok = await verify_totp_code(redis, user.id, secret, code)
    if not ok and _looks_like_recovery_code(code):
        ok = await _try_recovery_code(db, user.id, code)
    if not ok:
        # The second factor counts towards the same lockout as the
        # password, and a challenge burns after a few wrong codes.
        _register_failure(user, settings)
        attempts_left = await rate_limit.allow(
            redis,
            f"rl:totp:challenge:{token_hash}",
            _TOTP_ATTEMPTS_PER_CHALLENGE,
            settings.login_challenge_ttl_seconds,
        )
        if not attempts_left:
            challenge.consumed_at = _now()
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


_RECOVERY_CODE_RE = re.compile(r"^[0-9a-f]{10}-[0-9a-f]{10}$")


def _looks_like_recovery_code(code: str) -> bool:
    """Only a recovery-shaped code is checked against the recovery hashes:
    every check is an argon2 verification per stored code, and a six-digit
    guess must not buy eight of them."""
    return bool(_RECOVERY_CODE_RE.match(code.strip().lower()))


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
    normalized = code.strip().lower()
    for row in codes:
        if await asyncio.to_thread(verify_password, row.code_hash, normalized):
            row.used_at = _now()
            await db.flush()
            return True
    return False
