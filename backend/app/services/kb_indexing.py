"""Document indexing: chunk -> embed -> store, run by the worker.

Embeddings are never generated inside an HTTP request: the API only
marks a document `pending` and the worker picks it up. Draft and
archived documents are not indexed at all (SPEC §11), so importing a
pile of drafts costs nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from app.ai.tokens import estimate_tokens
from app.models import KbChunk, KbDocument, KbProcessingLog
from app.models.enums import DocStatus
from app.services.kb_text import chunk_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.ai.embeddings import EmbeddingProvider
    from app.config import Settings

logger = structlog.get_logger(__name__)

_EMBED_BATCH = 32


def is_indexable(document: KbDocument) -> bool:
    return (
        document.status is DocStatus.ACTIVE
        and document.is_searchable
        and document.deleted_at is None
    )


def mark_for_indexing(document: KbDocument) -> None:
    """Called from the API whenever content or eligibility changed."""
    document.index_state = "pending" if is_indexable(document) else "not_indexed"
    document.index_error = None


async def index_document(
    db: AsyncSession,
    document: KbDocument,
    provider: EmbeddingProvider,
    settings: Settings,
) -> int:
    """(Re)build the chunks of one document. Returns the chunk count."""
    if not is_indexable(document):
        # Eligibility changed while queued: drop stale chunks and stop.
        await db.execute(delete(KbChunk).where(KbChunk.document_id == document.id))
        document.index_state = "not_indexed"
        document.indexed_at = None
        return 0

    pieces = chunk_text(document.body, settings.chunk_tokens, settings.chunk_overlap)
    if not pieces:
        await db.execute(delete(KbChunk).where(KbChunk.document_id == document.id))
        document.index_state = "indexed"
        document.indexed_at = datetime.now(UTC)
        db.add(
            KbProcessingLog(
                document_id=document.id,
                step="index",
                status="empty",
                detail="документ не содержит текста для индексации",
            )
        )
        return 0

    vectors: list[list[float]] = []
    total_tokens = 0
    model = provider.model
    for start in range(0, len(pieces), _EMBED_BATCH):
        batch = pieces[start : start + _EMBED_BATCH]
        result = await provider.embed(batch)
        vectors.extend(result.vectors)
        total_tokens += result.total_tokens
        model = result.model

    # Replace atomically inside the caller's transaction: a failed embed
    # above leaves the old chunks untouched.
    await db.execute(delete(KbChunk).where(KbChunk.document_id == document.id))
    metadata_base = {
        "document_id": document.id,
        "title": document.title,
        "category": document.category,
        "language": document.language.value,
        "version": document.version,
        "valid_until": document.valid_until.isoformat() if document.valid_until else None,
    }
    for index, (content, vector) in enumerate(zip(pieces, vectors, strict=True)):
        db.add(
            KbChunk(
                document_id=document.id,
                chunk_index=index,
                content=content,
                token_count=estimate_tokens(content),
                embedding=vector,
                doc_metadata={**metadata_base, "chunk_index": index},
                embedding_model=model,
            )
        )
    document.index_state = "indexed"
    document.indexed_at = datetime.now(UTC)
    document.index_error = None
    db.add(
        KbProcessingLog(
            document_id=document.id,
            step="index",
            status="ok",
            detail=f"чанков: {len(pieces)}, модель: {model}",
            tokens_used=total_tokens,
        )
    )
    logger.info(
        "kb_document_indexed",
        document_id=document.id,
        chunks=len(pieces),
        tokens=total_tokens,
    )
    return len(pieces)


async def process_pending_documents(
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: EmbeddingProvider,
    settings: Settings,
    limit: int = 5,
) -> int:
    """Worker step: index queued documents, one transaction PER document.

    One document = at most one paid embedding attempt per pass: ANY
    failure (not just EmbeddingError) rolls back that document's own
    transaction and marks it `failed` in a fresh one. A shared batch
    transaction would abort on the first bad document and leave it
    `pending`, making the worker re-pay for the same embeddings forever.
    """
    async with sessionmaker() as db:
        ids = (
            (
                await db.execute(
                    select(KbDocument.id)
                    .where(KbDocument.index_state == "pending")
                    .order_by(KbDocument.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    handled = 0
    for document_id in ids:
        handled += await _index_one(sessionmaker, document_id, provider, settings)
    return handled


async def _index_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    document_id: int,
    provider: EmbeddingProvider,
    settings: Settings,
) -> int:
    try:
        async with sessionmaker() as db:
            document = (
                await db.execute(
                    select(KbDocument)
                    .where(KbDocument.id == document_id, KbDocument.index_state == "pending")
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if document is None:
                # Re-checked under lock: taken by a concurrent pass or no
                # longer pending — nothing to do, nothing paid.
                return 0
            await index_document(db, document, provider, settings)
            await db.commit()
            return 1
    except Exception as exc:  # noqa: BLE001 — every failure must end in `failed`
        logger.exception("kb_index_failed", document_id=document_id)
        await _mark_failed(sessionmaker, document_id, exc)
        return 1


async def _mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: int, exc: Exception
) -> None:
    detail = (str(exc) or type(exc).__name__)[:300]
    try:
        async with sessionmaker() as db:
            document = await db.get(KbDocument, document_id)
            if document is None:
                return
            document.index_state = "failed"
            document.index_error = detail
            db.add(
                KbProcessingLog(
                    document_id=document_id,
                    step="index",
                    status="failed",
                    detail=detail,
                )
            )
            await db.commit()
    except Exception:
        # Marking failed itself failed (DB down mid-pass): the document
        # stays pending and will be retried once the DB is back.
        logger.exception("kb_index_mark_failed_error", document_id=document_id)
