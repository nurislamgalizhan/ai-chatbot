"""E.164 phone normalization. Server-side only — the client is never trusted."""

from __future__ import annotations

import re

_ALLOWED_CHARS_RE = re.compile(r"[\s\-().]")
_E164_RE = re.compile(r"^\+[1-9]\d{9,14}$")


class PhoneValidationError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Normalize user input to E.164 or raise PhoneValidationError.

    Accepts common formatting (spaces, dashes, parentheses) and the local
    Kazakhstan/Russia convention of a leading 8 for +7 numbers.
    """
    cleaned = _ALLOWED_CHARS_RE.sub("", raw.strip())
    if cleaned.startswith("8") and len(cleaned) == 11:  # noqa: PLR2004
        cleaned = "+7" + cleaned[1:]
    if not _E164_RE.fullmatch(cleaned):
        msg = "Номер телефона должен быть в международном формате, например +77011234567"
        raise PhoneValidationError(msg)
    return cleaned
