"""First-line filter for images that must never reach OpenAI.

SPEC §13 forbids automatic analysis of ID documents, bank cards, medical
imaging and anything with sensitive personal data. This module decides
with FREE local signals only:
- words in the caption (the strongest signal in practice: people say
  what they are sending), in ru/kk/en;
- a card-like aspect ratio for tight scans/screenshots.

It is deliberately a filter, not a guarantee: a photo of a passport with
no caption looks like any other photo. That limitation is the reason
documents as a file type are never read at all, and the reason the
verdict on doubt is "give it to a human" (SPEC: «при сомнении — человек»).
"""

from __future__ import annotations

import re
import unicodedata

# Kept separate from kb_text normalization: this list must stay readable
# and auditable by the owner, and it is matched on word boundaries so
# "картина" does not trigger the "карта" rule.
_SENSITIVE_WORDS: tuple[str, ...] = (
    # ru — identity documents
    "паспорт",
    "паспорта",
    "удостоверение",
    "удостоверения",
    "уд лич",
    "иин",
    "инн",
    "снилс",
    "внж",
    "виза",
    # ru — payment
    "карта",
    "карты",
    "карточка",
    "карточки",
    "cvv",
    "cvc",
    "пин",
    "реквизиты",
    "выписка",
    "чек",
    "квитанция",
    "перевод",
    "оплата",
    "оплатил",
    "оплатила",
    "счет",
    "счёт",
    # ru — medical / legal
    "справка",
    "справку",
    "медсправка",
    "анализ",
    "анализы",
    "снимок",
    "снимки",
    "рентген",
    "мрт",
    "флюорография",
    "диагноз",
    "рецепт",
    "договор",
    "контракт",
    "доверенность",
    # kk
    "жеке куалик",
    "жеке куәлік",
    "куәлік",
    "төлқұжат",
    "толкужат",
    "анықтама",
    "аныктама",
    "шот",
    "түбіртек",
    "тубиртек",
    "келісімшарт",
    "шарт",
    "дәрігер",
    "darige",
    # en
    "passport",
    "id card",
    "identity",
    "driver license",
    "driving licence",
    "credit card",
    "debit card",
    "bank statement",
    "invoice",
    "receipt",
    "prescription",
    "diagnosis",
    "medical",
    "x-ray",
    "xray",
    "contract",
)

# Digit runs that look like a card number (16 digits, possibly spaced or
# dashed) or an IIN/passport-length run — a strong signal in a caption.
_CARD_NUMBER_RE = re.compile(r"(?:\d[ -]?){13,19}")
_LONG_DIGIT_RUN_RE = re.compile(r"\d{9,}")


def _normalize(text: str) -> str:
    lowered = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)


def caption_is_sensitive(caption: str | None) -> str | None:
    """Matched sensitive marker, or None. The RETURN VALUE IS A REASON
    CODE for logs — never the customer's text (PII stays out of logs)."""
    if not caption or not caption.strip():
        return None
    normalized = _normalize(caption)
    padded = f" {normalized} "
    for word in _SENSITIVE_WORDS:
        if f" {_normalize(word).strip()} " in padded:
            return "caption_keyword"
    if _CARD_NUMBER_RE.search(caption):
        return "caption_card_number"
    if _LONG_DIGIT_RUN_RE.search(caption):
        return "caption_long_digits"
    return None


def image_is_sensitive(caption: str | None, *, looks_like_card_scan: bool) -> str | None:
    """Verdict for one image: a reason code to refuse vision, or None.

    Order matters only for which reason is reported; either signal is
    enough to keep the file away from OpenAI.
    """
    reason = caption_is_sensitive(caption)
    if reason is not None:
        return reason
    if looks_like_card_scan:
        return "card_like_crop"
    return None
