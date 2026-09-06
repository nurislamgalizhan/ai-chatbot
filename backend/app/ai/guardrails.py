"""Pre-model heuristics and post-model output validation (SPEC §14.1).

These are the cheap outer belt: an obvious injection or off-topic request
is refused by a template WITHOUT paying for a model call. The inner belt
is the security block in the system instruction; neither replaces the
other. Customer text and RAG chunks are always DATA, never instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.kb_text import normalize_query

# Canary baked into the stable security block: if the model output ever
# contains it, the system prompt leaked and the answer must be replaced.
PROMPT_CANARY = "[[internal-guard-7f3a]]"

MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 4000

# Normalized substrings (see normalize_query) — deliberately blunt: false
# positives cost one templated reply, false negatives cost money.
_INJECTION_MARKERS = (
    "игнорируй инструкции",
    "игнорируй все инструкции",
    "игнорируй предыдущие инструкции",
    "забудь инструкции",
    "забудь все инструкции",
    "покажи промпт",
    "покажи свой промпт",
    "покажи инструкции",
    "какой у тебя промпт",
    "системный промпт",
    "system prompt",
    "ignore previous instructions",
    "ignore all instructions",
    "disregard your instructions",
    "developer mode",
    "jailbreak",
    "ты теперь не бот",
    "представь что ты не",
)

_OFFTOPIC_MARKERS = (
    "напиши код",
    "напиши программу",
    "напиши эссе",
    "напиши сочинение",
    "напиши статью",
    "реши задачу",
    "реши уравнение",
    "переведи текст",
    "write code",
    "write an essay",
    "solve this problem",
)

_URL_RE = re.compile(r"https?://([^\s/]+)", re.IGNORECASE)


@dataclass(frozen=True)
class InputVerdict:
    ok: bool
    reason: str | None = None  # "injection" | "offtopic" | "too_long"


@dataclass(frozen=True)
class OutputVerdict:
    ok: bool
    reason: str | None = None  # "prompt_leak" | "too_long" | "link_not_allowed"


def check_input(text: str) -> InputVerdict:
    if len(text) > MAX_QUESTION_CHARS:
        return InputVerdict(ok=False, reason="too_long")
    normalized = normalize_query(text)
    for marker in _INJECTION_MARKERS:
        if marker in normalized:
            return InputVerdict(ok=False, reason="injection")
    for marker in _OFFTOPIC_MARKERS:
        if marker in normalized:
            return InputVerdict(ok=False, reason="offtopic")
    return InputVerdict(ok=True)


def check_output(text: str, *, allowed_link_domains: list[str]) -> OutputVerdict:
    """The model's answer must not leak the prompt, exceed sane length or
    link anywhere outside the owner's allowlist."""
    if PROMPT_CANARY in text:
        return OutputVerdict(ok=False, reason="prompt_leak")
    if len(text) > MAX_ANSWER_CHARS:
        return OutputVerdict(ok=False, reason="too_long")
    for match in _URL_RE.finditer(text):
        host = match.group(1).lower().rstrip(".,);")
        allowed = any(
            host == domain or host.endswith(f".{domain}")
            for domain in (d.lower() for d in allowed_link_domains)
        )
        if not allowed:
            return OutputVerdict(ok=False, reason="link_not_allowed")
    return OutputVerdict(ok=True)
