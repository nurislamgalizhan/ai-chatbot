"""Knowledge-base API: documents, chunks, FAQ, translation review,
language-miss analytics and a retrieval preview.

Indexing never happens in a request: endpoints mark documents `pending`
and the worker embeds them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingError
from app.models import (
    AdminUser,
    KbChunk,
    KbDocument,
    KbFaq,
    KbFaqMiss,
    KbFaqPattern,
    KbProcessingLog,
)
from app.models.enums import AnswerStatus, DocStatus, KbLanguage, SourceType
from app.schemas.kb import (
    ChunkItem,
    ChunkListResponse,
    DocumentCreateRequest,
    DocumentDetail,
    DocumentListItem,
    DocumentListResponse,
    DocumentUpdateRequest,
    FaqActivateRequest,
    FaqAnswerUpdateRequest,
    FaqCreateRequest,
    FaqItem,
    FaqListResponse,
    FaqPatternCreateRequest,
    FaqPatternItem,
    LanguageMissResponse,
    LanguageMissRow,
    ProcessingLogItem,
    ProcessingLogResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    TranslationReviewItem,
    TranslationReviewResponse,
)
from app.security import rate_limit
from app.security.audit import record_audit
from app.security.deps import client_ip, get_app_settings, get_db, get_redis, require_admin
from app.services.kb_faq import build_pattern, match_faq, missing_pattern_languages
from app.services.kb_indexing import mark_for_indexing
from app.services.kb_search import search_chunks
from app.services.kb_text import detect_language, normalize_query, token_key

router = APIRouter(prefix="/api/kb", tags=["kb"], dependencies=[Depends(require_admin)])

DOC_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
FAQ_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись FAQ не найдена")

_ANSWER_FIELDS = {
    "ru": ("answer_ru", "answer_ru_status"),
    "kk": ("answer_kk", "answer_kk_status"),
    "en": ("answer_en", "answer_en_status"),
}


async def _get_document(db: AsyncSession, document_id: int) -> KbDocument:
    document = await db.get(KbDocument, document_id)
    if document is None or document.deleted_at is not None:
        raise DOC_NOT_FOUND
    return document


async def _get_faq(db: AsyncSession, faq_id: int) -> KbFaq:
    faq = await db.get(KbFaq, faq_id)
    if faq is None:
        raise FAQ_NOT_FOUND
    return faq


async def _chunk_counts(db: AsyncSession, document_ids: list[int]) -> dict[int, int]:
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(KbChunk.document_id, func.count())
            .where(KbChunk.document_id.in_(document_ids))
            .group_by(KbChunk.document_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def _document_item(document: KbDocument, chunk_count: int) -> DocumentListItem:
    return DocumentListItem(
        id=document.id,
        title=document.title,
        category=document.category,
        language=document.language.value,
        status=document.status.value,
        version=document.version,
        is_searchable=document.is_searchable,
        valid_until=document.valid_until,
        index_state=document.index_state,
        index_error=document.index_error,
        chunk_count=chunk_count,
        updated_at=document.updated_at,
    )


# --------------------------- documents ---------------------------


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    category: Annotated[str | None, Query(max_length=60)] = None,
) -> DocumentListResponse:
    stmt = select(KbDocument).where(KbDocument.deleted_at.is_(None))
    if status_filter:
        stmt = stmt.where(KbDocument.status == status_filter)
    if category:
        stmt = stmt.where(KbDocument.category == category)
    documents = (
        (await db.execute(stmt.order_by(KbDocument.category, KbDocument.title))).scalars().all()
    )
    counts = await _chunk_counts(db, [d.id for d in documents])
    return DocumentListResponse(items=[_document_item(d, counts.get(d.id, 0)) for d in documents])


@router.post("/documents", response_model=DocumentDetail)
async def create_document(
    body: DocumentCreateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentDetail:
    document = KbDocument(
        title=body.title.strip(),
        body=body.body,
        category=body.category,
        language=KbLanguage(body.language),
        source_type=SourceType.MANUAL_TEXT,
        status=DocStatus(body.status),
        is_searchable=body.is_searchable,
        valid_until=body.valid_until,
        author_user_id=user.id,
    )
    mark_for_indexing(document)
    db.add(document)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Документ с таким названием в этой категории уже есть",
        ) from exc
    await record_audit(
        db,
        action="kb.document_created",
        actor_user_id=user.id,
        entity_type="kb_document",
        entity_id=str(document.id),
        after={"title": document.title, "status": document.status.value},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return DocumentDetail(
        **_document_item(document, 0).model_dump(),
        body=document.body,
        created_at=document.created_at,
    )


@router.get("/documents/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> DocumentDetail:
    document = await _get_document(db, document_id)
    counts = await _chunk_counts(db, [document.id])
    return DocumentDetail(
        **_document_item(document, counts.get(document.id, 0)).model_dump(),
        body=document.body,
        created_at=document.created_at,
    )


@router.patch("/documents/{document_id}", response_model=DocumentDetail)
async def update_document(
    document_id: int,
    body: DocumentUpdateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DocumentDetail:
    document = await _get_document(db, document_id)
    before = {
        "status": document.status.value,
        "is_searchable": document.is_searchable,
        "version": document.version,
    }
    changes = body.model_dump(exclude_unset=True)
    content_changed = False
    for field, value in changes.items():
        if field == "language" and value is not None:
            document.language = KbLanguage(value)
        elif field == "status" and value is not None:
            document.status = DocStatus(value)
        else:
            setattr(document, field, value)
        if field in {"body", "title", "category", "language"}:
            content_changed = True
    if content_changed:
        document.version += 1
    # Any of these can change RAG eligibility or content -> reindex.
    if content_changed or {"status", "is_searchable"} & changes.keys():
        mark_for_indexing(document)
    await db.flush()
    await record_audit(
        db,
        action="kb.document_updated",
        actor_user_id=user.id,
        entity_type="kb_document",
        entity_id=str(document.id),
        before=before,
        after={
            "status": document.status.value,
            "is_searchable": document.is_searchable,
            "version": document.version,
        },
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    counts = await _chunk_counts(db, [document.id])
    return DocumentDetail(
        **_document_item(document, counts.get(document.id, 0)).model_dump(),
        body=document.body,
        created_at=document.created_at,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: int,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft delete: the row stays (restorable), chunks go so nothing
    deleted can ever surface in retrieval."""
    document = await _get_document(db, document_id)
    document.deleted_at = datetime.now(UTC)
    document.index_state = "not_indexed"
    await db.execute(delete(KbChunk).where(KbChunk.document_id == document.id))
    await record_audit(
        db,
        action="kb.document_deleted",
        actor_user_id=user.id,
        entity_type="kb_document",
        entity_id=str(document.id),
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )


@router.post("/documents/{document_id}/reindex", response_model=DocumentDetail)
async def reindex_document(
    document_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> DocumentDetail:
    document = await _get_document(db, document_id)
    mark_for_indexing(document)
    await db.flush()
    counts = await _chunk_counts(db, [document.id])
    return DocumentDetail(
        **_document_item(document, counts.get(document.id, 0)).model_dump(),
        body=document.body,
        created_at=document.created_at,
    )


@router.get("/documents/{document_id}/chunks", response_model=ChunkListResponse)
async def list_chunks(
    document_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> ChunkListResponse:
    await _get_document(db, document_id)
    chunks = (
        (
            await db.execute(
                select(KbChunk)
                .where(KbChunk.document_id == document_id)
                .order_by(KbChunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    return ChunkListResponse(
        items=[
            ChunkItem(
                id=c.id,
                chunk_index=c.chunk_index,
                content=c.content,
                token_count=c.token_count,
                embedding_model=c.embedding_model,
            )
            for c in chunks
        ]
    )


@router.get("/documents/{document_id}/log", response_model=ProcessingLogResponse)
async def document_log(
    document_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> ProcessingLogResponse:
    await _get_document(db, document_id)
    rows = (
        (
            await db.execute(
                select(KbProcessingLog)
                .where(KbProcessingLog.document_id == document_id)
                .order_by(KbProcessingLog.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return ProcessingLogResponse(
        items=[
            ProcessingLogItem(
                step=r.step,
                status=r.status,
                detail=r.detail,
                tokens_used=r.tokens_used,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


# ------------------------------ FAQ ------------------------------


async def _faq_items(db: AsyncSession, faqs: list[KbFaq]) -> list[FaqItem]:
    if not faqs:
        return []
    patterns = (
        (
            await db.execute(
                select(KbFaqPattern)
                .where(KbFaqPattern.faq_id.in_([f.id for f in faqs]))
                .order_by(KbFaqPattern.language, KbFaqPattern.id)
            )
        )
        .scalars()
        .all()
    )
    by_faq: dict[int, list[FaqPatternItem]] = {}
    for pattern in patterns:
        by_faq.setdefault(pattern.faq_id, []).append(
            FaqPatternItem(
                id=pattern.id,
                language=pattern.language.value,
                pattern=pattern.pattern,
            )
        )
    return [
        FaqItem(
            id=faq.id,
            normalized_key=faq.normalized_key,
            category=faq.category,
            is_active=faq.is_active,
            answer_ru=faq.answer_ru,
            answer_ru_status=faq.answer_ru_status.value,
            answer_kk=faq.answer_kk,
            answer_kk_status=faq.answer_kk_status.value,
            answer_en=faq.answer_en,
            answer_en_status=faq.answer_en_status.value,
            patterns=by_faq.get(faq.id, []),
            updated_at=faq.updated_at,
        )
        for faq in faqs
    ]


@router.get("/faq", response_model=FaqListResponse)
async def list_faq(db: Annotated[AsyncSession, Depends(get_db)]) -> FaqListResponse:
    faqs = (await db.execute(select(KbFaq).order_by(KbFaq.normalized_key))).scalars().all()
    return FaqListResponse(items=await _faq_items(db, list(faqs)))


@router.post("/faq", response_model=FaqItem)
async def create_faq(
    body: FaqCreateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaqItem:
    """A new entry starts inactive with an approved ru answer; kk/en
    stay absent until a human approves a translation."""
    faq = KbFaq(
        normalized_key=body.normalized_key,
        category=body.category,
        answer_ru=body.answer_ru,
        answer_ru_status=AnswerStatus.APPROVED,
        is_active=False,
    )
    db.add(faq)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Запись с таким ключом уже существует",
        ) from exc

    for language, patterns in (
        (KbLanguage.RU, body.patterns_ru),
        (KbLanguage.KK, body.patterns_kk),
        (KbLanguage.EN, body.patterns_en),
    ):
        for pattern in patterns:
            db.add(build_pattern(faq.id, language, pattern))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Такая формулировка уже ведёт к другой записи FAQ",
        ) from exc

    await record_audit(
        db,
        action="kb.faq_created",
        actor_user_id=user.id,
        entity_type="kb_faq",
        entity_id=str(faq.id),
        after={"key": faq.normalized_key},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return (await _faq_items(db, [faq]))[0]


@router.patch("/faq/{faq_id}/answer", response_model=FaqItem)
async def update_faq_answer(
    faq_id: int,
    body: FaqAnswerUpdateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaqItem:
    """One entry, one language — deliberately not a bulk endpoint:
    the owner's policy forbids mass approval of translations."""
    faq = await _get_faq(db, faq_id)
    answer_field, status_field = _ANSWER_FIELDS[body.language]
    new_status = AnswerStatus(body.status)
    answer = (body.answer or "").strip() or None
    if new_status is AnswerStatus.ABSENT:
        answer = None
    elif answer is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Пустой ответ нельзя пометить как machine или approved",
        )
    if body.language == "ru" and new_status is not AnswerStatus.APPROVED and faq.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="У активной записи русский ответ должен быть утверждён",
        )
    before = {"status": getattr(faq, status_field).value}
    setattr(faq, answer_field, answer)
    setattr(faq, status_field, new_status)
    await db.flush()
    await record_audit(
        db,
        action="kb.faq_answer_updated",
        actor_user_id=user.id,
        entity_type="kb_faq",
        entity_id=str(faq.id),
        before=before,
        after={"language": body.language, "status": new_status.value},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return (await _faq_items(db, [faq]))[0]


@router.post("/faq/{faq_id}/patterns", response_model=FaqItem)
async def add_faq_pattern(
    faq_id: int,
    body: FaqPatternCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaqItem:
    faq = await _get_faq(db, faq_id)
    db.add(build_pattern(faq.id, KbLanguage(body.language), body.pattern))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Такая формулировка уже используется",
        ) from exc
    return (await _faq_items(db, [faq]))[0]


@router.delete("/faq/{faq_id}/patterns/{pattern_id}", response_model=FaqItem)
async def delete_faq_pattern(
    faq_id: int, pattern_id: int, db: Annotated[AsyncSession, Depends(get_db)]
) -> FaqItem:
    faq = await _get_faq(db, faq_id)
    pattern = await db.get(KbFaqPattern, pattern_id)
    if pattern is None or pattern.faq_id != faq_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Формулировка не найдена")
    await db.delete(pattern)
    await db.flush()
    return (await _faq_items(db, [faq]))[0]


@router.post("/faq/{faq_id}/activate", response_model=FaqItem)
async def activate_faq(
    faq_id: int,
    body: FaqActivateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FaqItem:
    faq = await _get_faq(db, faq_id)
    if body.is_active:
        if faq.answer_ru_status is not AnswerStatus.APPROVED:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Нельзя включить запись без утверждённого русского ответа",
            )
        missing = await missing_pattern_languages(db, faq.id)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Нужны формулировки на языках: {', '.join(missing)}",
            )
    faq.is_active = body.is_active
    await db.flush()
    await record_audit(
        db,
        action="kb.faq_activated" if body.is_active else "kb.faq_deactivated",
        actor_user_id=user.id,
        entity_type="kb_faq",
        entity_id=str(faq.id),
        after={"is_active": body.is_active},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return (await _faq_items(db, [faq]))[0]


@router.get("/faq/translations", response_model=TranslationReviewResponse)
async def translation_review(
    db: Annotated[AsyncSession, Depends(get_db)],
    language: Annotated[str, Query(pattern="^(kk|en)$")] = "kk",
    status_filter: Annotated[
        str, Query(alias="status", pattern="^(machine|absent|approved|all)$")
    ] = "machine",
) -> TranslationReviewResponse:
    """Review screen: the ru source next to the draft for one language.
    Approval happens one entry at a time via PATCH /faq/{id}/answer."""
    answer_field, status_field = _ANSWER_FIELDS[language]
    stmt = select(KbFaq).order_by(KbFaq.normalized_key)
    if status_filter != "all":
        stmt = stmt.where(getattr(KbFaq, status_field) == status_filter)
    faqs = (await db.execute(stmt)).scalars().all()

    miss_rows = (
        await db.execute(
            select(KbFaqMiss.faq_id, func.count())
            .where(KbFaqMiss.language == language)
            .group_by(KbFaqMiss.faq_id)
        )
    ).all()
    misses: dict[int | None, int] = {row[0]: row[1] for row in miss_rows}
    return TranslationReviewResponse(
        items=[
            TranslationReviewItem(
                faq_id=faq.id,
                normalized_key=faq.normalized_key,
                category=faq.category,
                source_ru=faq.answer_ru,
                language=language,
                draft=getattr(faq, answer_field),
                status=getattr(faq, status_field).value,
                miss_count=misses.get(faq.id, 0),
            )
            for faq in faqs
        ]
    )


@router.get("/faq/language-misses", response_model=LanguageMissResponse)
async def language_misses(
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> LanguageMissResponse:
    """How often each language fell through to LLM for lack of an
    approved answer — the working list of what to translate next."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                KbFaqMiss.language,
                KbFaqMiss.faq_id,
                KbFaq.normalized_key,
                KbFaqMiss.category,
                func.count().label("misses"),
            )
            .join(KbFaq, KbFaq.id == KbFaqMiss.faq_id, isouter=True)
            .where(KbFaqMiss.created_at >= since)
            .group_by(
                KbFaqMiss.language, KbFaqMiss.faq_id, KbFaq.normalized_key, KbFaqMiss.category
            )
            .order_by(func.count().desc())
        )
    ).all()
    items = [
        LanguageMissRow(
            language=language.value,
            faq_id=faq_id,
            normalized_key=key,
            category=category,
            misses=count,
        )
        for language, faq_id, key, category, count in rows
    ]
    return LanguageMissResponse(items=items, total=sum(i.misses for i in items))


# ------------------------- retrieval preview -------------------------


# The preview embeds the query — a paid call. One admin account exists,
# so the limit is per-endpoint, protecting the wallet against a runaway
# tab or script, not enforcing per-user fairness.
_SEARCH_RATE_LIMIT_PER_WINDOW = 30
_SEARCH_RATE_WINDOW_SECONDS = 60


@router.post("/search", response_model=SearchResponse)
async def search_preview(
    body: SearchRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> SearchResponse:
    """Shows exactly what the pipeline would retrieve. Embeds the query
    (cheap) but never calls the chat model — SPEC invariant 16."""
    allowed = await rate_limit.allow(
        redis, "rl:kb:search", _SEARCH_RATE_LIMIT_PER_WINDOW, _SEARCH_RATE_WINDOW_SECONDS
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много запросов к поиску — подождите минуту",
        )
    language = detect_language(body.query)
    faq = await match_faq(db, body.query, language)
    faq_miss = faq is None and await _pattern_exists(db, body.query)

    provider = getattr(request.app.state, "embedding_provider", None)
    hits: list[SearchHit] = []
    if provider is not None:
        settings = get_app_settings(request)
        try:
            found = await search_chunks(db, provider, settings, body.query, category=body.category)
        except EmbeddingError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Поиск недоступен: не настроен ключ OpenAI",
            ) from exc
        hits = [
            SearchHit(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                title=h.title,
                content=h.content,
                score=round(h.score, 4),
            )
            for h in found
        ]
    return SearchResponse(
        faq_hit=faq.answer if faq else None,
        faq_language_miss=faq_miss,
        detected_language=language.value,
        hits=hits,
    )


async def _pattern_exists(db: AsyncSession, text: str) -> bool:
    normalized = normalize_query(text)
    tokens = token_key(text)
    found = (
        await db.execute(
            select(KbFaqPattern.id)
            .join(KbFaq, KbFaq.id == KbFaqPattern.faq_id)
            .where(
                KbFaq.is_active.is_(True),
                (KbFaqPattern.normalized_pattern == normalized)
                | (KbFaqPattern.token_key == tokens),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None
