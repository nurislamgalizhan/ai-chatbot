"""The offline stand-ins must be honest about what they are: lexical
similarity that separates related from unrelated text, and a model
stand-in that answers in the language the pipeline named."""

from __future__ import annotations

import math

import pytest

from app.ai.chat_provider import ChatRequest
from app.eval.offline import (
    OFFLINE_RAG_MIN_SCORE,
    LexicalEmbeddings,
    OracleChatProvider,
    lexical_tokens,
)
from app.eval.runner import REQUIRED_CATEGORIES, load_suite, same_numbers
from app.services.kb_text import detect_language

GYM = (
    "Абонементы: Разовый абонемент - 2000 тенге. Дневной: 12 посещений, время посещения "
    "07:00-17:00, стоимость 9000 тенге. Свободный: стоимость 13000 тенге."
)
RANGE = "Тир и страйкбол\nГеоргий Викторович - +7 705 600 76 02"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    )


def test_prefixes_bridge_word_forms() -> None:
    assert set(lexical_tokens("стоит зал")) & set(lexical_tokens("стоимость залов"))
    # Politeness noise and one-letter words carry no signal.
    assert lexical_tokens("здравствуйте, а вы") == []


def test_related_text_scores_above_threshold_and_unrelated_below() -> None:
    question = LexicalEmbeddings.vector("сколько стоит абонемент в зал")
    related = cosine(question, LexicalEmbeddings.vector(GYM))
    unrelated = cosine(question, LexicalEmbeddings.vector(RANGE))
    assert related > OFFLINE_RAG_MIN_SCORE > unrelated
    assert cosine(question, LexicalEmbeddings.vector("Есть ли у вас бассейн")) < 0.05


def test_vectors_are_unit_length_even_for_empty_text() -> None:
    for text in ("", "👍", GYM):
        vector = LexicalEmbeddings.vector(text)
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-6)


async def test_oracle_answers_in_the_language_the_pipeline_named() -> None:
    oracle = OracleChatProvider()
    request = ChatRequest(
        system="инструкция",
        messages=[
            {
                "role": "system",
                "content": "Язык клиента: kk (казахский). Отвечай на этом языке.\n\n"
                'Справочные данные:\n<kb_chunk id="1" source="Абонементы">'
                f"{GYM}</kb_chunk>",
            },
            {"role": "user", "content": "Абонемент қанша тұрады?"},
        ],
        model="fake",
        max_output_tokens=100,
        reasoning_effort="low",
    )
    result = await oracle.complete(request)
    assert "9000" in result.text
    assert detect_language(result.text).value == "kk"
    assert oracle.requests == [request]
    assert result.usage.input_tokens > 0


async def test_oracle_service_reply_follows_the_requested_language() -> None:
    oracle = OracleChatProvider()
    request = ChatRequest(
        system="сервис",
        messages=[{"role": "user", "content": "Ситуация: нет данных\nЯзык ответа: en"}],
        model="fake",
        max_output_tokens=60,
        reasoning_effort="none",
    )
    result = await oracle.complete(request)
    assert detect_language(result.text).value == "en"


@pytest.mark.parametrize(
    ("answer", "price"),
    [
        ("Абонемент стоит 9 000 тенге", "9000"),
        ("A membership is 9,000 tenge", "9000"),
        ("Аренда 26 000 тенге/час", "26000"),
        ("Карта на год — 108 000 тенге", "108000"),
    ],
)
def test_a_price_is_found_however_the_model_formats_it(answer: str, price: str) -> None:
    """The first live run failed twelve CORRECT answers because the model
    writes «9 000 тенге» and the criterion says «9000»."""
    assert same_numbers(price) in same_numbers(answer)


@pytest.mark.parametrize(
    ("text", "unchanged"),
    [
        ("Занятие длится 1,5 часа", "1,5"),
        ("Зал на 20 человек", "20 человек"),
        ("Пн, Ср, Пт: 15:00–17:00", "15:00"),
    ],
)
def test_normalisation_leaves_everything_but_thousands_alone(text: str, unchanged: str) -> None:
    """A decimal comma and a plain space are not thousands separators:
    turning «1,5» into «15» would invent a price that was never said."""
    assert unchanged in same_numbers(text)


def test_suite_file_is_valid_and_covers_the_spec() -> None:
    suite = load_suite()
    assert len(suite.cases) >= 40
    assert {case.category for case in suite.cases} >= REQUIRED_CATEGORIES
    mandatory_kk = [c for c in suite.cases if c.mandatory and c.language == "kk"]
    assert mandatory_kk, "owner decision: Kazakh question without approved_kk"
    assert any("faq_miss" in c.expect.model_dump(exclude_none=True) for c in mandatory_kk)


@pytest.mark.parametrize("case", load_suite().cases, ids=lambda c: c.id)
def test_every_case_is_written_in_the_language_it_claims(case) -> None:
    assert detect_language(case.input).value == case.language
