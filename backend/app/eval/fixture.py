"""Seed data for the offline eval and the load test.

Only ever written into a scratch database (see `scratch.py`). The
knowledge content mirrors the owner's real import (the Savvi export) so
the questions in the eval set are the questions real customers ask; the
FAQ entries mirror the ones the owner created in the panel, with the
translation statuses the acceptance cases need (approved / machine).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.models import FxRate, KbChunk, KbDocument, KbFaq, WhatsappInstance
from app.models.enums import AnswerStatus, DocStatus, KbLanguage, SourceType
from app.services import app_settings
from app.services.agent import activate_version, create_version
from app.services.app_settings import set_setting
from app.services.kb_faq import build_pattern
from app.services.kb_indexing import process_pending_documents
from app.services.pricing import seed_default_prices
from app.whatsapp.receiver import GREEN_API_TOKEN_REF

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.ai.embeddings import EmbeddingProvider
    from app.config import Settings

#: Registered under this id so nothing can be confused with a real
#: instance; the transport is never started by the acceptance tools.
FIXTURE_INSTANCE_ID = "0000000000"
FIXTURE_USD_KZT = Decimal(500)


class FixtureDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    category: str
    body: str
    searchable: bool = True


class FixtureAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    status: Literal["machine", "approved"] = "approved"


class FixtureFaq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    category: str | None = None
    answer_ru: str
    answer_kk: FixtureAnswer | None = None
    answer_en: FixtureAnswer | None = None
    patterns: dict[Literal["ru", "kk", "en"], list[str]] = Field(default_factory=dict)


class EvalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    style: str | None = None
    transcription_terms: str = ""
    documents: list[FixtureDocument]
    faq: list[FixtureFaq]


@dataclass(frozen=True)
class SeededFixture:
    instance_pk: int
    instruction_version: int
    documents: int
    indexed: int
    chunks: int
    faq: int


def _status(answer: FixtureAnswer | None) -> AnswerStatus:
    if answer is None:
        return AnswerStatus.ABSENT
    return AnswerStatus.APPROVED if answer.status == "approved" else AnswerStatus.MACHINE


async def _seed_faq(db: AsyncSession, entries: list[FixtureFaq]) -> int:
    for entry in entries:
        faq = KbFaq(
            normalized_key=entry.key,
            category=entry.category,
            answer_ru=entry.answer_ru,
            answer_ru_status=AnswerStatus.APPROVED,
            answer_kk=entry.answer_kk.text if entry.answer_kk else None,
            answer_kk_status=_status(entry.answer_kk),
            answer_en=entry.answer_en.text if entry.answer_en else None,
            answer_en_status=_status(entry.answer_en),
            is_active=True,
        )
        db.add(faq)
        await db.flush()
        for language, patterns in entry.patterns.items():
            for pattern in patterns:
                db.add(build_pattern(faq.id, KbLanguage(language), pattern))
    return len(entries)


async def seed_fixture(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    fixture: EvalFixture,
    embeddings: EmbeddingProvider,
) -> SeededFixture:
    """Everything the paid path needs: money, instruction, knowledge."""
    async with sessionmaker() as db, db.begin():
        instance = WhatsappInstance(
            provider="green_api", instance_id=FIXTURE_INSTANCE_ID, token_ref=GREEN_API_TOKEN_REF
        )
        db.add(instance)
        await db.flush()
        db.add(FxRate(pair="USD/KZT", rate=FIXTURE_USD_KZT, effective_from=datetime.now(UTC)))
        await seed_default_prices(db, settings)
        # Debounce is a production courtesy (a customer typing in several
        # bubbles); the acceptance runs measure the pipeline, not typing.
        await set_setting(db, app_settings.DEBOUNCE_SECONDS, 0)
        if fixture.transcription_terms:
            await set_setting(db, app_settings.TRANSCRIPTION_TERMS, fixture.transcription_terms)
        instruction = await create_version(
            db,
            system_prompt=fixture.instruction,
            style=fixture.style,
            escalation_rules=None,
            max_output_tokens=None,
            reasoning_effort=None,
            author_user_id=None,
        )
        await activate_version(db, instruction, actor_user_id=None)
        faq_count = await _seed_faq(db, fixture.faq)
        for document in fixture.documents:
            db.add(
                KbDocument(
                    title=document.title,
                    category=document.category,
                    language=KbLanguage.RU,
                    source_type=SourceType.MANUAL_TEXT,
                    status=DocStatus.ACTIVE,
                    is_searchable=document.searchable,
                    body=document.body,
                    index_state="pending" if document.searchable else "not_indexed",
                )
            )
        instance_pk = instance.id
        version = instruction.version

    indexed_total = 0
    while True:
        handled = await process_pending_documents(sessionmaker, embeddings, settings, limit=20)
        if handled == 0:
            break
        indexed_total += handled

    async with sessionmaker() as db:
        chunks = (await db.execute(select(func.count()).select_from(KbChunk))).scalar_one()
        indexed = (
            await db.execute(
                select(func.count())
                .select_from(KbDocument)
                .where(KbDocument.index_state == "indexed")
            )
        ).scalar_one()
    return SeededFixture(
        instance_pk=instance_pk,
        instruction_version=version,
        documents=len(fixture.documents),
        indexed=int(indexed),
        chunks=int(chunks),
        faq=faq_count,
    )
