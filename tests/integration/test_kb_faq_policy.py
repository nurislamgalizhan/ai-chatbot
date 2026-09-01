"""FAQ language policy (owner decision 2026-08-31).

The rules that must never bend:
- a `machine` draft is NEVER delivered to a customer;
- the Russian answer is NEVER substituted for a Kazakh/English question;
- such a fall-through is recorded as a language miss and the caller goes
  to RAG+LLM in the customer's language;
- an entry cannot go active without an approved ru answer and ru+kk
  patterns.
"""

from __future__ import annotations

import pytest
from app.models import KbFaq, KbFaqMiss
from app.models.enums import AnswerStatus, KbLanguage
from app.services.kb_faq import approved_answer, build_pattern, match_faq
from app.services.kb_text import detect_language
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

RU_ANSWER = "Адрес: г. Алматы, ул. Коперника 130."
KK_ANSWER = "Мекенжайы: Алматы қаласы, Коперник көшесі 130."


async def seed_faq(
    db_engine,
    *,
    kk_status: AnswerStatus = AnswerStatus.ABSENT,
    kk_answer: str | None = None,
    is_active: bool = True,
) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        faq = KbFaq(
            normalized_key="address",
            category="org",
            answer_ru=RU_ANSWER,
            answer_ru_status=AnswerStatus.APPROVED,
            answer_kk=kk_answer,
            answer_kk_status=kk_status,
            is_active=is_active,
        )
        db.add(faq)
        await db.flush()
        db.add(build_pattern(faq.id, KbLanguage.RU, "какой у вас адрес"))
        db.add(build_pattern(faq.id, KbLanguage.KK, "мекенжайыңыз қандай"))
        return faq.id


async def test_russian_question_gets_approved_russian_answer(db_engine) -> None:
    await seed_faq(db_engine)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(db, "Какой у вас адрес?", KbLanguage.RU)
    assert hit is not None
    assert hit.answer == RU_ANSWER


async def test_kazakh_question_without_approved_kk_falls_through_and_records_miss(
    db_engine,
) -> None:
    """THE core rule: no Russian text for a Kazakh question — the caller
    goes to RAG+LLM in Kazakh instead, and the miss is recorded."""
    faq_id = await seed_faq(db_engine)
    question = "Мекенжайыңыз қандай?"
    assert detect_language(question) is KbLanguage.KK

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(db, question, KbLanguage.KK, chat_id=None)
        await db.commit()

    assert hit is None  # fall through to RAG+LLM, NOT the Russian answer
    async with maker() as db:
        misses = (await db.execute(select(KbFaqMiss))).scalars().all()
    assert len(misses) == 1
    assert misses[0].language is KbLanguage.KK
    assert misses[0].faq_id == faq_id


async def test_machine_draft_is_never_delivered(db_engine) -> None:
    """A machine translation exists in the row but must stay invisible to
    customers — only the admin review screen may show it."""
    await seed_faq(
        db_engine, kk_status=AnswerStatus.MACHINE, kk_answer="Машинный черновик"
    )
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(db, "Мекенжайыңыз қандай?", KbLanguage.KK)
        await db.commit()
        misses = (await db.execute(select(KbFaqMiss))).scalars().all()
    assert hit is None
    assert len(misses) == 1  # counted as a miss, exactly like `absent`


async def test_approved_kk_answer_is_delivered(db_engine) -> None:
    await seed_faq(db_engine, kk_status=AnswerStatus.APPROVED, kk_answer=KK_ANSWER)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(db, "Мекенжайыңыз қандай?", KbLanguage.KK)
        await db.commit()
        misses = (await db.execute(select(KbFaqMiss))).scalars().all()
    assert hit is not None
    assert hit.answer == KK_ANSWER
    assert misses == []  # an approved answer is not a miss


async def test_inactive_faq_never_matches(db_engine) -> None:
    await seed_faq(db_engine, is_active=False)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        assert await match_faq(db, "Какой у вас адрес?", KbLanguage.RU) is None


async def test_unrelated_question_is_not_a_language_miss(db_engine) -> None:
    """No pattern matched at all: that is not an FAQ question, so it must
    not pollute the translation backlog."""
    await seed_faq(db_engine)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(
            db, "Сколько стоит аренда поля на два часа?", KbLanguage.RU
        )
        await db.commit()
        misses = (await db.execute(select(KbFaqMiss))).scalars().all()
    assert hit is None
    assert misses == []


async def test_word_order_and_politeness_still_match(db_engine) -> None:
    await seed_faq(db_engine)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hit = await match_faq(
            db, "Здравствуйте! Подскажите, адрес какой?", KbLanguage.RU
        )
    assert hit is not None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (AnswerStatus.APPROVED, RU_ANSWER),
        (AnswerStatus.MACHINE, None),
        (AnswerStatus.ABSENT, None),
    ],
)
def test_approved_answer_helper(status: AnswerStatus, expected: str | None) -> None:
    faq = KbFaq(answer_ru=RU_ANSWER, answer_ru_status=status)
    assert approved_answer(faq, KbLanguage.RU) == expected
