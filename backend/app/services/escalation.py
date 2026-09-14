"""When a human must see the chat even though the bot answered.

The model has no tools (SPEC invariant 6), so it cannot raise a flag
itself; and an FAQ answer never reaches the model at all. Two things a
customer says therefore have to be recognised by the pipeline from plain
word lists:

* they ask for a person («позовите администратора», «менеджер керек»,
  "I want to talk to a human");
* they want to book or sign up («забронировать зал на субботу»,
  «брондау», "book the field") — the owner's rule is that every booking
  goes to a manager (prepayment), the bot only explains.

The answer still goes out (the FAQ text or the model's reply); the
dialog additionally gets `needs_attention`, which is how the manager
finds it. Word-prefix matching handles Russian/Kazakh morphology
(бронь / забронировать / бронирование) without a stemmer.
"""

from __future__ import annotations

from app.services.kb_text import normalize_query

HUMAN_REQUESTED = "human_requested"
BOOKING = "booking"

#: Cyrillic word prefixes: a word STARTING with one of these counts.
_HUMAN_PREFIXES = (
    "администратор",
    "админ",
    "менеджер",
    "оператор",
    "әкімші",
    "басшы",
)
#: Whole phrases matched inside the normalized text ("человек" alone
#: would fire on «зал на 20 человек»).
_HUMAN_PHRASES = (
    "живой человек",
    "с человеком",
    "живым человеком",
    "адаммен",
    "talk to a human",
    "real person",
    "speak to someone",
)
_HUMAN_WORDS_LATIN = frozenset({"manager", "administrator", "operator", "human"})

_BOOKING_PREFIXES = (
    "брон",
    "заброн",
    "переброн",
    "резерв",
    "зарезерв",
    "заявк",
    "записат",
    "записыв",
    "запиши",
    "запишите",
    "брондау",
    "брондап",
    "тапсырыс",
    "тіркел",
    "жазыл",
)
_BOOKING_WORDS_LATIN = frozenset(
    {"book", "booking", "reserve", "reservation", "appointment", "enroll", "enrol", "register"}
)
_BOOKING_PHRASES = ("sign up", "sign me up")


def escalation_reason(text: str) -> str | None:
    """Why a manager should look at this message, or None."""
    normalized = normalize_query(text)
    if not normalized:
        return None
    words = normalized.split()
    if any(phrase in normalized for phrase in _HUMAN_PHRASES) or any(
        word.startswith(_HUMAN_PREFIXES) or word in _HUMAN_WORDS_LATIN for word in words
    ):
        return HUMAN_REQUESTED
    if any(phrase in normalized for phrase in _BOOKING_PHRASES) or any(
        word.startswith(_BOOKING_PREFIXES) or word in _BOOKING_WORDS_LATIN for word in words
    ):
        return BOOKING
    return None
