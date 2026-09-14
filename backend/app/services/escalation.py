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
finds it.

Matching is by WHOLE normalized words, never by prefix: the first
version used prefixes and fired on «бронежилет», «бронзовая медаль»,
«резервуар» and «административное здание» — everyday words for a
guard-training centre with a sports hall.
"""

from __future__ import annotations

from app.services.kb_text import normalize_query

HUMAN_REQUESTED = "human_requested"
BOOKING = "booking"

_HUMAN_WORDS = frozenset(
    {
        # ru — a person, in the cases a customer actually uses
        "администратор",
        "администратора",
        "администратору",
        "администратором",
        "администраторы",
        "менеджер",
        "менеджера",
        "менеджеру",
        "менеджером",
        "менеджеры",
        # nominative «оператор» is left out: «оператор связи не ловит»
        "оператора",
        "оператору",
        "оператором",
        # kk
        "әкімші",
        "әкімшіні",
        "әкімшіге",
        "әкімшімен",
        "менеджерге",
        "менеджермен",
        "операторға",
        # en
        "manager",
        "administrator",
    }
)
#: Whole phrases matched inside the normalized text («человек» alone
#: would fire on «зал на 20 человек»).
_HUMAN_PHRASES = (
    "живой человек",
    "живым человеком",
    "с человеком",
    "адаммен сөйлес",
    "talk to a human",
    "talk to a person",
    "real person",
    "speak to someone",
)

_BOOKING_WORDS = frozenset(
    {
        # ru — бронь / бронирование / записаться / заявка
        "бронь",
        "брони",
        "бронью",
        "бронирование",
        "бронирования",
        "бронировать",
        "забронировать",
        "забронируйте",
        "забронирую",
        "забронируем",
        "перебронировать",
        "зарезервировать",
        "зарезервируйте",
        "заявка",
        "заявку",
        "заявки",
        "записаться",
        "запишите",
        "запиши",
        "записать",
        "запишусь",
        # kk
        "брондау",
        "брондаңыз",
        "брондап",
        "тапсырыс",
        "жазылу",
        "жазылғым",
        "жазылайын",
        "жазыңыз",
        "тіркелу",
        "тіркеңіз",
        # en
        "book",
        "booking",
        "reserve",
        "reservation",
        "appointment",
        "enroll",
        "enrol",
        "register",
    }
)
_BOOKING_PHRASES = ("sign up", "sign me up")


def escalation_reason(text: str) -> str | None:
    """Why a manager should look at this message, or None."""
    normalized = normalize_query(text)
    if not normalized:
        return None
    words = set(normalized.split())
    if words & _HUMAN_WORDS or any(phrase in normalized for phrase in _HUMAN_PHRASES):
        return HUMAN_REQUESTED
    if words & _BOOKING_WORDS or any(phrase in normalized for phrase in _BOOKING_PHRASES):
        return BOOKING
    return None
