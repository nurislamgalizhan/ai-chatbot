"""Showing that a secret exists without showing the secret.

SPEC §12.9: tokens and keys are never displayed, "даже частично, кроме
последних 4 символов". One helper, used everywhere, so that rule is not
re-implemented (and re-broken) per page.
"""

from __future__ import annotations

#: Below this length the "last four" of a value would be most of it.
_MIN_LENGTH_TO_HINT = 8


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """`abcd...wxyz` -> `••••wxyz`; short or empty values reveal nothing."""
    if not value:
        return ""
    if len(value) < _MIN_LENGTH_TO_HINT:
        return "••••"
    return f"••••{value[-keep:]}"
