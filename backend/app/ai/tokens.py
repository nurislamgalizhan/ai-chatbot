"""Token estimation for chunking.

Deliberate simplification for phase 5: chunk sizing does not need
token-exact precision — a chunk that is 430 or 470 tokens behaves the
same. Phase 6, where tokens are money, adds an exact tokenizer for the
budget path; this estimator is never used to decide spending.

Calibration: OpenAI BPE averages ~4 chars/token for Latin text and
~2.5 chars/token for Cyrillic (Russian/Kazakh cost more per character).
"""

from __future__ import annotations

_CYRILLIC_CHARS_PER_TOKEN = 2.5
_OTHER_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cyrillic = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    other = len(text) - cyrillic
    estimate = cyrillic / _CYRILLIC_CHARS_PER_TOKEN + other / _OTHER_CHARS_PER_TOKEN
    return max(1, round(estimate))


# --- phase 6: exact counting for context trimming ---
# Billing never uses these numbers (costs come from factual API usage);
# exactness only makes the MAX_INPUT_TOKENS trim honest.

_ENCODING = None
_ENCODING_FAILED = False


def count_tokens(text: str) -> int:
    """Exact o200k_base count; falls back to the estimate (with a logged
    warning, once) if the encoding cannot be loaded — the worker must not
    die for lack of tokenizer data, and the trim stays conservative."""
    global _ENCODING, _ENCODING_FAILED  # noqa: PLW0603 - module-level cache
    if _ENCODING is None and not _ENCODING_FAILED:
        try:
            import tiktoken  # noqa: PLC0415 - lazy: the API process never counts

            _ENCODING = tiktoken.get_encoding("o200k_base")
        except Exception:  # pragma: no cover - only without baked cache/network
            _ENCODING_FAILED = True
            import structlog  # noqa: PLC0415

            structlog.get_logger(__name__).warning(
                "tiktoken_unavailable", detail="falling back to estimate_tokens"
            )
    if _ENCODING is None:  # pragma: no cover
        return estimate_tokens(text)
    return len(_ENCODING.encode(text))
