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

from app.ai.embeddings import EmbeddingError
from app.ai.tokens import estimate_tokens
from app.models import KbChunk, KbDocument, KbProcessingLog
from app.models.enums import DocStatus
from app.services.kb_text import chunk_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    db: AsyncSession, provider: EmbeddingProvider, settings: Settings, limit: int = 5
) -> int:
    """Worker step: index queued documents one transaction at a time."""
    documents = (
        (
            await db.execute(
                select(KbDocument)
                .where(KbDocument.index_state == "pending")
                .order_by(KbDocument.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for document in documents:
        try:
            await index_document(db, document, provider, settings)
        except EmbeddingError as exc:
            # Honest failure state: visible in the admin, retried manually.
            document.index_state = "failed"
            document.index_error = str(exc)[:300]
            db.add(
                KbProcessingLog(
                    document_id=document.id,
                    step="index",
                    status="failed",
                    detail=str(exc)[:300],
                )
            )
            logger.error("kb_index_failed", document_id=document.id, error=str(exc))
    return len(documents)
