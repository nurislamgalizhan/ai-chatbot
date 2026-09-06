"""Chunking, normalization and language detection."""

from __future__ import annotations

import pytest
from app.ai.tokens import estimate_tokens
from app.models.enums import KbLanguage
from app.services.kb_text import chunk_text, detect_language, normalize_query, token_key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Здравствуйте! Какой у вас адрес?", KbLanguage.RU),
        ("Сәлеметсіз бе, мекенжайыңыз қандай?", KbLanguage.KK),
        ("What is your address?", KbLanguage.EN),
        # Mixed kk/ru counts as kk: answering in the wrong language is
        # worse than one extra LLM call (owner policy).
        ("Здравствуйте, бағасы қанша?", KbLanguage.KK),
        ("", KbLanguage.RU),
        # A Kazakh name inside a Russian sentence must NOT flip the whole
        # message to kk (недочёт №2): below the ratio threshold it is ru,
        # so the FAQ answers in the language the client actually wrote.
        (
            "Здравствуйте, меня зовут Айгерім. Хочу записаться на секцию бокса",
            KbLanguage.RU,
        ),
        ("Меня зовут Ұлан, подскажите график работы зала пожалуйста", KbLanguage.RU),
        # A genuinely short kk question stays kk: the ratio is high even
        # with a single Kazakh-specific letter.
        ("қанша тұрады", KbLanguage.KK),
    ],
)
def test_detect_language(text: str, expected: KbLanguage) -> None:
    assert detect_language(text) is expected


def test_normalize_query_strips_noise() -> None:
    assert normalize_query("  Какой У ВАС адрес??? 😀 ") == "какой у вас адрес"


def test_token_key_is_order_and_politeness_insensitive() -> None:
    """ "адрес" and "подскажите, какой у вас адрес?" must match the same
    FAQ entry."""
    assert token_key("адрес") == token_key("Подскажите, какой у вас адрес?")
    assert token_key("график работы") == token_key("работы график!")
    # Different questions must NOT collide.
    assert token_key("адрес") != token_key("телефон")


def test_chunking_respects_size_and_keeps_paragraphs() -> None:
    paragraph = "Стоимость аренды зала составляет 26000 тенге в час. " * 12
    body = "\n\n".join([paragraph] * 4)
    chunks = chunk_text(body, chunk_tokens=200, overlap_tokens=40)

    assert len(chunks) > 1
    for chunk in chunks:
        # Overlap can push a chunk slightly over; a hard cap would split
        # mid-sentence, which is worse for retrieval.
        assert estimate_tokens(chunk) <= 200 * 1.5


def test_chunking_short_text_is_one_chunk() -> None:
    assert len(chunk_text("Адрес: улица Коперника 130.", 450, 60)) == 1


def test_chunking_overlap_carries_context() -> None:
    """Paragraphs smaller than the overlap budget are carried over, so a
    fact split across a boundary stays retrievable."""
    units = [f"Пункт номер {i}. Короткий факт про услугу." for i in range(12)]
    chunks = chunk_text("\n\n".join(units), chunk_tokens=100, overlap_tokens=60)
    assert len(chunks) >= 2
    tail_of_first = chunks[0].split("\n\n")[-1]
    assert tail_of_first in chunks[1]


def test_chunking_without_overlap_does_not_repeat() -> None:
    units = [f"Пункт номер {i}. Короткий факт про услугу." for i in range(12)]
    chunks = chunk_text("\n\n".join(units), chunk_tokens=100, overlap_tokens=0)
    tail_of_first = chunks[0].split("\n\n")[-1]
    assert tail_of_first not in chunks[1]


def test_chunking_empty_body() -> None:
    assert chunk_text("   \n\n  ", 450, 60) == []
