"""Semantic answer cache (SPEC §11), OFF by default.

The key is sha256(normalize_query(text)) + language + category +
kb_version; kb_version is a settings counter bumped by EVERY knowledge
mutation, so a KB edit invalidates the whole cache without a scan
(updated_at would miss valid_until expiry and soft deletes).

Never cached (design review §8): answers built with chat memory, answers
carrying personal data (phone/date/time patterns), templated answers,
answers to merged multi-messages, and answers with zero RAG chunks.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import AppSetting, SemanticCacheEntry
from app.services import app_settings
from app.services.app_settings import get_setting
from app.services.kb_text import normalize_query

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.enums import KbLanguage

# Personal-data smells in an answer: phone numbers, calendar dates, times.
_PII_RES = (
    re.compile(r"\+?\d[\d\s()-]{9,}\d"),  # phone-ish
    re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b"),  # 21.09 / 21.09.2026
    re.compile(r"\b\d{1,2}:\d{2}\b"),  # 15:30
)

_BUMP_KB_VERSION_SQL = text(
    """
    INSERT INTO settings (key, value, value_type)
    VALUES (:key, to_jsonb(1), 'int')
    ON CONFLICT (key) DO UPDATE
        SET value = to_jsonb(((settings.value)::text)::int + 1)
    RETURNING ((settings.value)::text)::int
    """
)


def query_hash(question: str) -> str:
    return hashlib.sha256(normalize_query(question).encode()).hexdigest()


def answer_carries_personal_data(answer: str) -> bool:
    return any(pattern.search(answer) for pattern in _PII_RES)


async def get_kb_version(db: AsyncSession) -> int:
    return int(await get_setting(db, app_settings.KB_VERSION))


async def bump_kb_version(db: AsyncSession) -> int:
    """Atomic increment; called by every kb_documents/kb_faq mutation."""
    result = await db.execute(_BUMP_KB_VERSION_SQL, {"key": app_settings.KB_VERSION})
    row = result.scalar_one_or_none()
    if row is None:
        # Row existed and was updated: RETURNING on upsert always returns,
        # so None means dialect surprise — re-read defensively.
        stored = await db.get(AppSetting, app_settings.KB_VERSION)
        return int(stored.value) if stored else 0
    return int(row)


async def lookup(
    db: AsyncSession,
    question: str,
    language: KbLanguage,
    category: str | None,
    kb_version: int,
) -> str | None:
    now = datetime.now(UTC)
    entry = (
        await db.execute(
            select(SemanticCacheEntry).where(
                SemanticCacheEntry.normalized_query_hash == query_hash(question),
                SemanticCacheEntry.language == language,
                SemanticCacheEntry.category == (category or ""),
                SemanticCacheEntry.kb_version == kb_version,
                SemanticCacheEntry.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    await db.execute(
        update(SemanticCacheEntry)
        .where(SemanticCacheEntry.id == entry.id)
        .values(hit_count=SemanticCacheEntry.hit_count + 1)
    )
    return entry.answer


async def store(
    db: AsyncSession,
    question: str,
    language: KbLanguage,
    category: str | None,
    kb_version: int,
    answer: str,
    ttl_hours: int,
) -> bool:
    """Idempotent insert; the CALLER enforces the never-cache rules —
    this function only refuses answers with obvious personal data as the
    last line of defense."""
    if answer_carries_personal_data(answer):
        return False
    inserted = (
        await db.execute(
            pg_insert(SemanticCacheEntry)
            .values(
                normalized_query_hash=query_hash(question),
                language=language,
                category=category or "",
                kb_version=kb_version,
                answer=answer,
                expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            )
            .on_conflict_do_nothing(constraint="uq_semantic_cache_key")
            .returning(SemanticCacheEntry.id)
        )
    ).scalar_one_or_none()
    return inserted is not None
