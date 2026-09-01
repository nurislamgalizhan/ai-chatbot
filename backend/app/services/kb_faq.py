"""Direct FAQ answers and the owner's language policy.

Policy (owner decision 2026-08-31), enforced in one place:
- ru is the source of truth; kk patterns are mandatory alongside ru;
- an answer is delivered ONLY if its status for the customer's language
  is `approved`;
- a `machine` draft is never delivered — not as a fallback, not "just
  this once"; it exists solely for review in the admin;
- no approved answer in the customer's language => return None so the
  caller falls through to RAG+LLM *in that language*. The Russian text
  is never substituted for a Kazakh question: spending ~0.25 ₸ on an
  LLM answer beats answering in the wrong language;
- every such fall-through is recorded as a language miss — the working
  list of what to translate next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.models import KbFaq, KbFaqMiss, KbFaqPattern
from app.models.enums import AnswerStatus, KbLanguage
from app.services.kb_text import normalize_query, token_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_ANSWER_FIELDS: dict[KbLanguage, tuple[str, str]] = {
    KbLanguage.RU: ("answer_ru", "answer_ru_status"),
    KbLanguage.KK: ("answer_kk", "answer_kk_status"),
    KbLanguage.EN: ("answer_en", "answer_en_status"),
}


@dataclass(frozen=True)
class FaqHit:
    faq_id: int
    answer: str
    language: KbLanguage


def approved_answer(faq: KbFaq, language: KbLanguage) -> str | None:
    """The ONLY place an FAQ answer is extracted. Returns None unless the
    status for that language is `approved`."""
    answer_field, status_field = _ANSWER_FIELDS[language]
    if getattr(faq, status_field) is not AnswerStatus.APPROVED:
        return None
    answer: str | None = getattr(faq, answer_field)
    return answer or None


async def match_faq(
    db: AsyncSession, text: str, language: KbLanguage, chat_id: int | None = None
) -> FaqHit | None:
    """Find an active FAQ for this question, or None to use RAG+LLM."""
    normalized = normalize_query(text)
    if not normalized:
        return None
    tokens = token_key(text)

    faq = await _find_by_pattern(db, normalized, tokens)
    if faq is None:
        return None  # not an FAQ question at all — not a language miss

    answer = approved_answer(faq, language)
    if answer is None:
        # Pattern matched but this language has no approved answer.
        db.add(
            KbFaqMiss(
                faq_id=faq.id,
                language=language,
                category=faq.category,
                chat_id=chat_id,
            )
        )
        logger.info("faq_language_miss", faq_id=faq.id, language=language.value)
        return None
    return FaqHit(faq_id=faq.id, answer=answer, language=language)


async def _find_by_pattern(db: AsyncSession, normalized: str, tokens: list[str]) -> KbFaq | None:
    """Exact normalized match first, then an order-insensitive word-set
    match. Both are deterministic — no fuzzy scoring that could answer a
    different question than the one asked."""
    exact = (
        await db.execute(
            select(KbFaq)
            .join(KbFaqPattern, KbFaqPattern.faq_id == KbFaq.id)
            .where(KbFaq.is_active.is_(True), KbFaqPattern.normalized_pattern == normalized)
            .limit(1)
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact
    if not tokens:
        return None
    return (
        await db.execute(
            select(KbFaq)
            .join(KbFaqPattern, KbFaqPattern.faq_id == KbFaq.id)
            .where(KbFaq.is_active.is_(True), KbFaqPattern.token_key == tokens)
            .limit(1)
        )
    ).scalar_one_or_none()


def build_pattern(faq_id: int, language: KbLanguage, pattern: str) -> KbFaqPattern:
    return KbFaqPattern(
        faq_id=faq_id,
        language=language,
        pattern=pattern.strip(),
        normalized_pattern=normalize_query(pattern),
        token_key=token_key(pattern),
    )


async def missing_pattern_languages(db: AsyncSession, faq_id: int) -> list[str]:
    """ru and kk patterns are mandatory before an entry may go active."""
    rows = (
        (await db.execute(select(KbFaqPattern.language).where(KbFaqPattern.faq_id == faq_id)))
        .scalars()
        .all()
    )
    present = {row.value if hasattr(row, "value") else str(row) for row in rows}
    return [lang for lang in ("ru", "kk") if lang not in present]
