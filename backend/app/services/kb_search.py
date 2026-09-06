"""RAG retrieval: metadata filter -> vector search -> threshold -> dedup.

Hard rule (SPEC invariant 2): the whole knowledge base is NEVER sent to
the model. Only chunks above the relevance threshold, at most top_k, and
only from documents that are active, searchable, not deleted and not
expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import or_, select

from app.models import KbChunk, KbDocument
from app.models.enums import DocStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import EmbeddingProvider
    from app.config import Settings
    from app.models.enums import KbLanguage

logger = structlog.get_logger(__name__)

# Two chunks whose contents overlap this much are near-duplicates.
_DEDUP_JACCARD = 0.8


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    document_id: int
    title: str
    content: str
    score: float
    category: str | None


@dataclass(frozen=True)
class SearchOutcome:
    """Hits plus the factual embedding usage — the query embed is a paid
    call and must be accountable in ai_calls (phase 6)."""

    hits: list[RetrievedChunk]
    embed_tokens: int
    embed_model: str


async def search_chunks(
    db: AsyncSession,
    provider: EmbeddingProvider,
    settings: Settings,
    query: str,
    *,
    language: KbLanguage | None = None,
    category: str | None = None,
    top_k: int | None = None,
    min_score: float | None = None,
) -> SearchOutcome:
    """Return the chunks worth putting into a prompt (possibly none)."""
    limit = top_k or settings.rag_top_k
    threshold = settings.rag_min_score if min_score is None else min_score

    embedded = await provider.embed([query])
    vector = embedded.vectors[0]

    now = datetime.now(UTC)
    distance = KbChunk.embedding.cosine_distance(vector).label("distance")
    stmt = (
        select(KbChunk, KbDocument, distance)
        .join(KbDocument, KbDocument.id == KbChunk.document_id)
        .where(
            KbDocument.status == DocStatus.ACTIVE,
            KbDocument.is_searchable.is_(True),
            KbDocument.deleted_at.is_(None),
            or_(KbDocument.valid_until.is_(None), KbDocument.valid_until > now),
        )
        # Over-fetch so dedup cannot starve the result below top_k.
        .order_by(distance)
        .limit(limit * 3)
    )
    if category is not None:
        stmt = stmt.where(KbDocument.category == category)
    if language is not None:
        stmt = stmt.where(KbDocument.language == language)

    rows = (await db.execute(stmt)).all()

    results: list[RetrievedChunk] = []
    for chunk, document, dist in rows:
        # pgvector cosine distance in [0, 2]; similarity in [-1, 1].
        score = 1.0 - float(dist)
        if score < threshold:
            continue
        candidate = RetrievedChunk(
            chunk_id=chunk.id,
            document_id=document.id,
            title=document.title,
            content=chunk.content,
            score=score,
            category=document.category,
        )
        if _is_near_duplicate(candidate, results):
            continue
        results.append(candidate)
        if len(results) >= limit:
            break

    logger.info(
        "kb_search",
        candidates=len(rows),
        returned=len(results),
        threshold=threshold,
        category=category,
    )
    return SearchOutcome(
        hits=results, embed_tokens=embedded.total_tokens, embed_model=embedded.model
    )


def _is_near_duplicate(candidate: RetrievedChunk, accepted: list[RetrievedChunk]) -> bool:
    words = set(candidate.content.split())
    if not words:
        return True
    for existing in accepted:
        other = set(existing.content.split())
        if not other:
            continue
        overlap = len(words & other) / len(words | other)
        if overlap >= _DEDUP_JACCARD:
            return True
    return False
