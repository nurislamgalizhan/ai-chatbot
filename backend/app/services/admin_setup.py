"""Creation and maintenance of the single administrator account.

Used by the management CLI (`make create-admin`), never exposed over HTTP:
registration does not exist as an endpoint by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models import AdminUser, TotpRecoveryCode
from app.security.audit import record_audit
from app.security.passwords import hash_password, validate_password_strength
from app.security.sessions import revoke_all_sessions
from app.security.totp import (
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    provisioning_uri,
)
from app.services.phone import normalize_phone

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings

TOTP_ISSUER = "WhatsApp Bot Admin"


class AdminSetupError(Exception):
    """Raised with a human-readable (Russian) message for the CLI."""


@dataclass(frozen=True)
class CreatedAdmin:
    user_id: int
    phone: str
    totp_secret: str
    totp_uri: str
    recovery_codes: list[str]


async def count_admins(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(AdminUser)) or 0)


async def count_active_admins(db: AsyncSession) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(AdminUser).where(AdminUser.is_active)) or 0
    )


async def create_admin(
    db: AsyncSession, settings: Settings, phone: str, full_name: str, password: str
) -> CreatedAdmin:
    if await count_admins(db) > 0:
        msg = (
            "Учётная запись уже существует. Регистрации второй не предусмотрено; "
            "для смены пароля используйте reset-admin-password."
        )
        raise AdminSetupError(msg)
    normalized = normalize_phone(phone)
    password_error = validate_password_strength(password)
    if password_error:
        raise AdminSetupError(password_error)
    if not full_name.strip():
        msg = "Имя не может быть пустым"
        raise AdminSetupError(msg)

    totp_secret = generate_totp_secret()
    user = AdminUser(
        phone=normalized,
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        totp_secret_encrypted=encrypt_totp_secret(settings, totp_secret),
    )
    db.add(user)
    await db.flush()

    recovery_codes = generate_recovery_codes()
    for code in recovery_codes:
        db.add(TotpRecoveryCode(user_id=user.id, code_hash=hash_password(code)))
    await db.flush()
    await record_audit(db, action="admin.created", entity_type="admin_user", entity_id=str(user.id))
    return CreatedAdmin(
        user_id=user.id,
        phone=normalized,
        totp_secret=totp_secret,
        totp_uri=provisioning_uri(totp_secret, normalized, TOTP_ISSUER),
        recovery_codes=recovery_codes,
    )


async def reset_admin_password(db: AsyncSession, password: str) -> str:
    """Set a new password for the single admin; revokes every session."""
    password_error = validate_password_strength(password)
    if password_error:
        raise AdminSetupError(password_error)
    user = (await db.execute(select(AdminUser))).scalar_one_or_none()
    if user is None:
        msg = "Учётная запись ещё не создана — используйте create-admin."
        raise AdminSetupError(msg)
    user.password_hash = hash_password(password)
    user.password_changed_at = datetime.now(UTC)
    user.failed_login_count = 0
    user.locked_until = None
    await revoke_all_sessions(db, user.id)
    await record_audit(
        db,
        action="admin.password_reset_cli",
        entity_type="admin_user",
        entity_id=str(user.id),
    )
    return user.phone
