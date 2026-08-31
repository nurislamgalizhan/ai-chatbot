"""argon2id password hashing with constant-work verification."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()  # argon2id by default

# Verified against when the phone number is unknown, so that "no such user"
# and "wrong password" take the same time and return the same response.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    target = password_hash if password_hash is not None else _DUMMY_HASH
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError):
        return False
    return password_hash is not None


def validate_password_strength(password: str) -> str | None:
    """Return an error message (Russian, shown in UI) or None if acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов"
    if password.strip() != password:
        return "Пароль не должен начинаться или заканчиваться пробелом"
    return None
