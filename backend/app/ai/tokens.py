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
