"""Text utilities for the knowledge base: normalization, language
detection and semantic chunking."""

from __future__ import annotations

import re
import unicodedata

from app.ai.tokens import estimate_tokens
from app.models.enums import KbLanguage

# Letters unique to Kazakh Cyrillic — the cheapest reliable kk signal.
# Cyrillic only — a Latin "h" here would misread English as Kazakh.
_KAZAKH_LETTERS = set("әғқңөұүіһӘҒҚҢӨҰҮІҺ")
# Share of Kazakh-specific letters among ALL Cyrillic letters required to
# call the text kk: a lone Kazakh letter (a name like «Айгерім» inside a
# Russian sentence) must not flip the whole message to kk.
_KK_RATIO_THRESHOLD = 0.05
_KK_MIN_LETTERS = 2
_KK_SHORT_MESSAGE_LETTERS = 10
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_LATIN_RE = re.compile(r"[a-zA-Z]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
# Politeness noise that must not change which FAQ a question matches.
_STOPWORDS = frozenset(
    {
        "а",
        "и",
        "в",
        "на",
        "у",
        "к",
        "с",
        "по",
        "за",
        "о",
        "об",
        "от",
        "до",
        "для",
        "же",
        "ли",
        "бы",
        "вы",
        "ваш",
        "ваша",
        "ваши",
        "вас",
        "мне",
        "я",
        "это",
        "как",
        "какой",
        "какая",
        "какие",
        "есть",
        "быть",
        "пожалуйста",
        "здравствуйте",
        "привет",
        "добрый",
        "день",
        "скажите",
        "подскажите",
        "хочу",
        "можно",
        "the",
        "a",
        "is",
        "are",
        "do",
        "does",
        "you",
        "your",
        "please",
        "hello",
        "hi",
        "what",
        "how",
    }
)
_MIN_CHUNK_TOKENS = 40


def normalize_query(text: str) -> str:
    """Lowercase, strip punctuation/emoji, collapse whitespace."""
    lowered = unicodedata.normalize("NFKC", text).casefold()
    without_punct = _PUNCT_RE.sub(" ", lowered)
    return _SPACE_RE.sub(" ", without_punct).strip()


def token_key(text: str) -> list[str]:
    """Sorted set of significant words: makes matching insensitive to
    word order and politeness noise ("какой у вас адрес" == "адрес")."""
    words = [w for w in normalize_query(text).split() if w and w not in _STOPWORDS]
    return sorted(set(words))


def detect_language(text: str) -> KbLanguage:
    """Cheap heuristic; the LLM path re-checks language itself.

    kk requires Kazakh-specific letters to reach _KK_RATIO_THRESHOLD of
    all Cyrillic letters: genuinely mixed kk/ru text still counts as kk
    (answering in the wrong language is worse than one extra LLM call —
    owner's language policy), but a single Kazakh letter carried by a
    name inside a Russian sentence stays ru. Otherwise Cyrillic means ru
    and Latin means en.
    """
    kazakh = sum(1 for ch in text if ch in _KAZAKH_LETTERS)
    # _CYRILLIC_RE covers the core а-я range only, so Kazakh-specific
    # letters are added on top to get the full Cyrillic count.
    total_cyrillic = len(_CYRILLIC_RE.findall(text)) + kazakh
    # The ratio alone let ONE letter flip a short Russian sentence
    # («Айгерім придёт завтра» — 1 of 19 is 5.3 %). A single Kazakh
    # letter counts only in a very short message («Сәлем», «Қанша?»),
    # where it is the message rather than a name inside one.
    enough_letters = kazakh >= _KK_MIN_LETTERS or (
        kazakh == 1 and total_cyrillic <= _KK_SHORT_MESSAGE_LETTERS
    )
    if enough_letters and total_cyrillic and kazakh / total_cyrillic >= _KK_RATIO_THRESHOLD:
        return KbLanguage.KK
    if total_cyrillic == 0 and _LATIN_RE.search(text):
        return KbLanguage.EN
    return KbLanguage.RU


def chunk_text(body: str, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Semantic chunking: split on blank lines, then on sentences, never
    mid-word. Overlap carries the tail of the previous chunk so a fact
    split across a boundary stays retrievable."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if estimate_tokens(paragraph) <= chunk_tokens:
            units.append(paragraph)
            continue
        # Oversized paragraph: fall back to sentence/line granularity.
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n", paragraph) if p.strip()]
        units.extend(parts or [paragraph])

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = estimate_tokens(unit)
        if current and current_tokens + unit_tokens > chunk_tokens:
            chunks.append("\n\n".join(current))
            tail = _overlap_tail(current, overlap_tokens)
            current = [*tail, unit]
            current_tokens = sum(estimate_tokens(u) for u in current)
            continue
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append("\n\n".join(current))

    # Merge a tiny trailing chunk back: a 5-token fragment retrieves badly.
    if len(chunks) > 1 and estimate_tokens(chunks[-1]) < _MIN_CHUNK_TOKENS:
        chunks[-2] = f"{chunks[-2]}\n\n{chunks[-1]}"
        chunks.pop()
    return chunks


def _overlap_tail(units: list[str], overlap_tokens: int) -> list[str]:
    if overlap_tokens <= 0:
        return []
    tail: list[str] = []
    total = 0
    for unit in reversed(units):
        unit_tokens = estimate_tokens(unit)
        if total + unit_tokens > overlap_tokens:
            break
        tail.insert(0, unit)
        total += unit_tokens
    return tail
