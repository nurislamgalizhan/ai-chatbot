"""Indexing and retrieval against real PostgreSQL + pgvector.

Relevance itself is asserted with hand-seeded vectors: the fake provider
hashes text, so "semantic" similarity would be meaningless. What matters
here is that the FILTERS, THRESHOLD and dedup behave exactly as SPEC §11
requires — an archived, unsearchable, deleted or expired document must
never reach a prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.ai.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResult,
    FakeEmbeddingProvider,
)
from app.models import KbChunk, KbDocument, KbProcessingLog
from app.models.enums import DocStatus, KbLanguage, SourceType
from app.services.kb_indexing import index_document, process_pending_documents
from app.services.kb_search import search_chunks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

DIMENSIONS = 1536


def unit_vector(seed: float) -> list[float]:
    """A deterministic vector whose similarity to others is predictable:
    the first two coordinates form a 2-D direction, the rest are zero."""
    import math

    angle = seed
    vec = [0.0] * DIMENSIONS
    vec[0] = math.cos(angle)
    vec[1] = math.sin(angle)
    return vec


class ScriptedEmbeddings(EmbeddingProvider):
    """Returns a preset vector per exact text; unknown texts get a
    far-away direction so they fall below the threshold."""

    model = "scripted-model"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        vectors = [self.mapping.get(t, unit_vector(3.14159)) for t in texts]
        return EmbeddingResult(
            vectors=vectors, model=self.model, total_tokens=len(texts)
        )


class BrokenEmbeddings(EmbeddingProvider):
    model = "broken"

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        msg = "embedding_failed:Timeout"
        raise EmbeddingError(msg)


class WrongDimensionEmbeddings(EmbeddingProvider):
    """Succeeds (and gets paid) but returns vectors PostgreSQL cannot
    store — models any failure that happens AFTER the money is spent."""

    model = "wrong-dim"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            vectors=[[0.0, 0.0, 0.0] for _ in texts],
            model=self.model,
            total_tokens=len(texts),
        )


async def make_document(
    db_engine,
    *,
    title: str,
    body: str,
    status: DocStatus = DocStatus.ACTIVE,
    is_searchable: bool = True,
    category: str | None = "gym",
    valid_until: datetime | None = None,
    deleted: bool = False,
) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        document = KbDocument(
            title=title,
            body=body,
            category=category,
            language=KbLanguage.RU,
            source_type=SourceType.MANUAL_TEXT,
            status=status,
            is_searchable=is_searchable,
            valid_until=valid_until,
            deleted_at=datetime.now(UTC) if deleted else None,
            index_state="pending",
        )
        db.add(document)
        await db.flush()
        return document.id


@pytest.fixture
def settings_rag(settings):
    return settings.model_copy(update={"chunk_tokens": 200, "chunk_overlap": 40})


async def test_indexing_creates_chunks_and_log(db_engine, settings_rag) -> None:
    doc_id = await make_document(
        db_engine,
        title="Абонементы",
        body="Разовый абонемент 2000 тенге.\n\nДневной 9000 тенге.",
    )
    provider = FakeEmbeddingProvider()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await process_pending_documents(maker, provider, settings_rag) == 1

    async with maker() as db:
        document = await db.get(KbDocument, doc_id)
        chunks = (
            (await db.execute(select(KbChunk).where(KbChunk.document_id == doc_id)))
            .scalars()
            .all()
        )
        log = (
            (
                await db.execute(
                    select(KbProcessingLog).where(KbProcessingLog.document_id == doc_id)
                )
            )
            .scalars()
            .all()
        )
    assert document.index_state == "indexed"
    assert document.indexed_at is not None
    assert len(chunks) >= 1
    assert chunks[0].embedding_model == provider.model
    assert chunks[0].doc_metadata["title"] == "Абонементы"
    assert log[-1].status == "ok"


async def test_reindexing_is_idempotent(db_engine, settings_rag) -> None:
    """A rerun replaces chunks instead of duplicating them (the unique
    (document_id, chunk_index) index makes duplication impossible)."""
    doc_id = await make_document(
        db_engine, title="Аренда", body="Зал 26000 тенге в час."
    )
    provider = FakeEmbeddingProvider()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    for _ in range(3):
        async with maker() as db, db.begin():
            document = await db.get(KbDocument, doc_id)
            document.index_state = "pending"
        await process_pending_documents(maker, provider, settings_rag)

    async with maker() as db:
        chunks = (
            (await db.execute(select(KbChunk).where(KbChunk.document_id == doc_id)))
            .scalars()
            .all()
        )
    assert len(chunks) == 1


async def test_draft_and_archived_documents_are_not_indexed(
    db_engine, settings_rag
) -> None:
    draft = await make_document(
        db_engine, title="Черновик", body="текст", status=DocStatus.DRAFT
    )
    archived = await make_document(
        db_engine, title="Архив", body="текст", status=DocStatus.ARCHIVED
    )
    provider = FakeEmbeddingProvider()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    await process_pending_documents(maker, provider, settings_rag)

    async with maker() as db:
        chunks = (await db.execute(select(KbChunk))).scalars().all()
        drafts = await db.get(KbDocument, draft)
        arch = await db.get(KbDocument, archived)
    assert chunks == []
    assert drafts.index_state == "not_indexed"
    assert arch.index_state == "not_indexed"
    assert provider.calls == []  # no embeddings paid for drafts


async def test_embedding_failure_marks_document_failed(db_engine, settings_rag) -> None:
    doc_id = await make_document(db_engine, title="Секции", body="Бокс, карате.")
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    await process_pending_documents(maker, BrokenEmbeddings(), settings_rag)

    async with maker() as db:
        document = await db.get(KbDocument, doc_id)
        log = (
            (
                await db.execute(
                    select(KbProcessingLog).where(KbProcessingLog.document_id == doc_id)
                )
            )
            .scalars()
            .all()
        )
    assert document.index_state == "failed"
    assert "embedding_failed" in document.index_error
    assert log[-1].status == "failed"


async def test_failure_after_paid_embed_costs_exactly_one_attempt(
    db_engine, settings_rag
) -> None:
    """Недочёт №1: a failure AFTER the paid embed call (chunks cannot be
    written) must mark the document `failed` after ONE attempt. Before the
    fix only EmbeddingError was caught, so the document stayed `pending`
    and the worker re-paid for the same embeddings every few seconds."""
    doc_id = await make_document(db_engine, title="Ломаный", body="Текст документа.")
    provider = WrongDimensionEmbeddings()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    assert await process_pending_documents(maker, provider, settings_rag) == 1
    assert provider.calls == 1  # paid exactly once

    async with maker() as db:
        document = await db.get(KbDocument, doc_id)
        log = (
            (
                await db.execute(
                    select(KbProcessingLog).where(KbProcessingLog.document_id == doc_id)
                )
            )
            .scalars()
            .all()
        )
    assert document.index_state == "failed"
    assert document.index_error
    assert log[-1].status == "failed"

    # The next worker pass must not touch it: nothing pending, no new pay.
    assert await process_pending_documents(maker, provider, settings_rag) == 0
    assert provider.calls == 1


async def _index_with(db_engine, settings_rag, provider, doc_id: int) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        document = await db.get(KbDocument, doc_id)
        await index_document(db, document, provider, settings_rag)
        await db.commit()


async def test_search_returns_only_relevant_above_threshold(
    db_engine, settings_rag
) -> None:
    close_body = "Абонемент в тренажерный зал стоит 9000 тенге."
    far_body = "Гостиница: койко-место 8000 тенге в сутки."
    close_id = await make_document(db_engine, title="Абонементы", body=close_body)
    far_id = await make_document(
        db_engine, title="Гостиница", body=far_body, category="hotel"
    )

    query = "сколько стоит абонемент"
    provider = ScriptedEmbeddings(
        {
            close_body: unit_vector(0.05),  # ~0.999 similarity to the query
            far_body: unit_vector(1.5),  # ~0.07 similarity — below threshold
            query: unit_vector(0.0),
        }
    )
    await _index_with(db_engine, settings_rag, provider, close_id)
    await _index_with(db_engine, settings_rag, provider, far_id)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hits = await search_chunks(db, provider, settings_rag, query)
    assert [h.title for h in hits] == ["Абонементы"]
    assert hits[0].score > 0.9


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": DocStatus.ARCHIVED}, "archived"),
        ({"is_searchable": False}, "switched off"),
        ({"deleted": True}, "soft deleted"),
        ({"valid_until": datetime.now(UTC) - timedelta(days=1)}, "expired"),
    ],
)
async def test_ineligible_documents_never_reach_retrieval(
    db_engine, settings_rag, kwargs, reason
) -> None:
    """SPEC §11: an archived / unsearchable / deleted / expired document
    must not surface even when its chunks exist in the table."""
    body = "Секретный текст, который не должен попасть в ответ."
    query = "секретный текст"
    provider = ScriptedEmbeddings({body: unit_vector(0.0), query: unit_vector(0.0)})

    doc_id = await make_document(db_engine, title="Документ", body=body)
    await _index_with(
        db_engine, settings_rag, provider, doc_id
    )  # indexed while eligible

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        document = await db.get(KbDocument, doc_id)
        for field, value in kwargs.items():
            setattr(document, field, datetime.now(UTC) if field == "deleted" else value)
        if kwargs.get("deleted"):
            document.deleted_at = datetime.now(UTC)

    async with maker() as db:
        # Sanity: the chunks are still physically there...
        assert (await db.execute(select(KbChunk))).scalars().all()
        # ...and still invisible to retrieval.
        hits = await search_chunks(db, provider, settings_rag, query)
    assert hits == [], f"{reason} document leaked into retrieval"


async def test_search_never_returns_more_than_top_k(db_engine, settings_rag) -> None:
    """Invariant 2: the whole knowledge base is never handed to the model."""
    query = "вопрос"
    mapping = {query: unit_vector(0.0)}
    for i in range(12):
        body = f"Уникальный факт номер {i} про услуги центра."
        mapping[body] = unit_vector(0.01 * i)
        doc_id = await make_document(db_engine, title=f"Документ {i}", body=body)
        provider = ScriptedEmbeddings(mapping)
        await _index_with(db_engine, settings_rag, provider, doc_id)

    provider = ScriptedEmbeddings(mapping)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hits = await search_chunks(db, provider, settings_rag, query)
    assert len(hits) <= settings_rag.rag_top_k


async def test_category_filter(db_engine, settings_rag) -> None:
    gym_body = "Абонемент 9000 тенге."
    hotel_body = "Койко-место 8000 тенге."
    query = "цена"
    provider = ScriptedEmbeddings(
        {
            gym_body: unit_vector(0.0),
            hotel_body: unit_vector(0.0),
            query: unit_vector(0.0),
        }
    )
    gym = await make_document(db_engine, title="Зал", body=gym_body, category="gym")
    hotel = await make_document(
        db_engine, title="Отель", body=hotel_body, category="hotel"
    )
    await _index_with(db_engine, settings_rag, provider, gym)
    await _index_with(db_engine, settings_rag, provider, hotel)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        hits = await search_chunks(db, provider, settings_rag, query, category="hotel")
    assert [h.title for h in hits] == ["Отель"]
