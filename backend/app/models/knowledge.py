from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, ForeignKey, Identity, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.chat import _enum
from app.models.enums import AnswerStatus, DocStatus, KbLanguage, SourceType

# text-embedding-3-small. Changing this requires a migration + reindex.
EMBEDDING_DIMENSIONS = 1536


class KbDocument(Base):
    """A knowledge-base document. Only active + searchable + not deleted
    documents ever reach RAG (SPEC §11). Blocks can be switched off
    without deleting them (owner requirement, mirrors Savvi)."""

    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    language: Mapped[KbLanguage] = mapped_column(_enum(KbLanguage, "kb_language"))
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType, "kb_source_type"))
    status: Mapped[DocStatus] = mapped_column(
        _enum(DocStatus, "kb_doc_status"), server_default=text("'draft'")
    )
    version: Mapped[int] = mapped_column(server_default=text("1"))
    valid_until: Mapped[datetime | None]
    is_searchable: Mapped[bool] = mapped_column(server_default=text("true"))
    body: Mapped[str] = mapped_column(Text)
    file_ref: Mapped[str | None] = mapped_column(Text)
    author_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    # not_indexed -> pending -> indexed | failed. The worker owns the
    # transitions; embeddings are never generated inside a request.
    index_state: Mapped[str] = mapped_column(Text, server_default=text("'not_indexed'"))
    indexed_at: Mapped[datetime | None]
    index_error: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KbChunk(Base):
    """An indexed slice of a document with its embedding."""

    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int]
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    doc_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)
    embedding_model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbFaq(Base):
    """Direct question->answer pairs answered without any model call.

    Language policy (owner decision 2026-08-31):
    - ru is the source of truth;
    - each language carries its own status: absent | machine | approved;
    - a `machine` draft is NEVER sent to a customer, only shown in the
      admin for review;
    - no approved answer in the customer's language => fall through to
      RAG+LLM in that language, never substitute the Russian text.

    Created EMPTY by migrations; the owner fills it in via the admin.
    """

    __tablename__ = "kb_faq"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    normalized_key: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)

    answer_ru: Mapped[str | None] = mapped_column(Text)
    answer_ru_status: Mapped[AnswerStatus] = mapped_column(
        _enum(AnswerStatus, "kb_answer_status"), server_default=text("'absent'")
    )
    answer_kk: Mapped[str | None] = mapped_column(Text)
    answer_kk_status: Mapped[AnswerStatus] = mapped_column(
        _enum(AnswerStatus, "kb_answer_status"), server_default=text("'absent'")
    )
    answer_en: Mapped[str | None] = mapped_column(Text)
    answer_en_status: Mapped[AnswerStatus] = mapped_column(
        _enum(AnswerStatus, "kb_answer_status"), server_default=text("'absent'")
    )

    is_active: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KbFaqPattern(Base):
    """Question phrasings that map to a FAQ entry, per language.
    ru + kk patterns are mandatory before an entry may be activated."""

    __tablename__ = "kb_faq_patterns"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    faq_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("kb_faq.id", ondelete="CASCADE"))
    language: Mapped[KbLanguage] = mapped_column(_enum(KbLanguage, "kb_language"))
    pattern: Mapped[str] = mapped_column(Text)  # as typed by the owner
    normalized_pattern: Mapped[str] = mapped_column(Text)  # exact-match key
    token_key: Mapped[list[str]] = mapped_column(ARRAY(Text))  # sorted word set
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbFaqMiss(Base):
    """Analytics: a question in language L found no approved answer and
    fell through to RAG+LLM. The working list of what to translate next.
    Never stores the customer's text (PII)."""

    __tablename__ = "kb_faq_misses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    faq_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("kb_faq.id", ondelete="SET NULL")
    )
    language: Mapped[KbLanguage] = mapped_column(_enum(KbLanguage, "kb_language"))
    category: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KbProcessingLog(Base):
    """Indexing/processing trail per document (visible in the admin)."""

    __tablename__ = "kb_processing_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("kb_documents.id", ondelete="CASCADE")
    )
    step: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
