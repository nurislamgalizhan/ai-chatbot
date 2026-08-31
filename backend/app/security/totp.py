"""TOTP (RFC 6238) with encrypted secret at rest and replay protection."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pyotp
from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.config import Settings

RECOVERY_CODE_COUNT = 8
_TOTP_REPLAY_TTL_SECONDS = 95  # covers the +/-1 verification window


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def encrypt_totp_secret(settings: Settings, secret: str) -> str:
    return Fernet(settings.encryption_key.encode()).encrypt(secret.encode()).decode()


def decrypt_totp_secret(settings: Settings, encrypted: str) -> str:
    return Fernet(settings.encryption_key.encode()).decrypt(encrypted.encode()).decode()


async def verify_totp_code(redis: Redis, user_id: int, secret: str, code: str) -> bool:
    """Verify a TOTP code and reject replays of an already-accepted code."""
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False
    # SET NX: the first acceptance of this exact code wins; a replay loses.
    accepted_first_time = await redis.set(
        f"totp_used:{user_id}:{code}", "1", ex=_TOTP_REPLAY_TTL_SECONDS, nx=True
    )
    return bool(accepted_first_time)


def generate_recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(5)}-{secrets.token_hex(5)}" for _ in range(RECOVERY_CODE_COUNT)]
