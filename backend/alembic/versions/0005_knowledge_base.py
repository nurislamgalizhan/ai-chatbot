"""knowledge base: documents, chunks, faq, patterns, misses, processing log

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 1536

kb_language = postgresql.ENUM("ru", "kk", "en", name="kb_language", create_type=False)
kb_answer_status = postgresql.ENUM(
    "absent", "machine", "approved", name="kb_answer_status", create_type=False
)
kb_doc_status = postgresql.ENUM(
    "draft", "active", "archived", name="kb_doc_status", create_type=False
)
kb_source_type = postgresql.ENUM(
    "manual_text", "file", "faq", name="kb_source_type", create_type=False
)

_ENUMS = (kb_language, kb_answer_status, kb_doc_status, kb_source_type)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("language", kb_language, nullable=False),
        sa.Column("source_type", kb_source_type, nullable=False),
        sa.Column("status", kb_doc_status, server_default=sa.text("'draft'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_searchable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("file_ref", sa.Text(), nullable=True),
        sa.Column(
            "author_user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "index_state", sa.Text(), server_default=sa.text("'not_indexed'"), nullable=False
        ),
        sa.Column("indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("index_error", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The RAG eligibility filter, as a partial index.
    op.create_index(
        "ix_kb_documents_searchable",
        "kb_documents",
        ["status", "is_searchable"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_kb_documents_category", "kb_documents", ["category"])
    # Worker pickup queue for indexing.
    op.create_index(
        "ix_kb_documents_pending",
        "kb_documents",
        ["index_state"],
        postgresql_where=sa.text("index_state = 'pending'"),
    )
    # Idempotent kb-import: one document per (title, category).
    op.create_index(
        "uq_kb_documents_title_category",
        "kb_documents",
        ["title", sa.text("coalesce(category, '')")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Reindexing is idempotent: a rerun cannot duplicate chunks.
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_kb_chunks_doc_index"),
    )
    # HNSW over cosine distance: the dataset is small (hundreds of chunks),
    # HNSW gives stable recall without tuning IVFFlat lists.
    op.execute(
        "CREATE INDEX ix_kb_chunks_embedding ON kb_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "kb_faq",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("normalized_key", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("answer_ru", sa.Text(), nullable=True),
        sa.Column(
            "answer_ru_status",
            kb_answer_status,
            server_default=sa.text("'absent'"),
            nullable=False,
        ),
        sa.Column("answer_kk", sa.Text(), nullable=True),
        sa.Column(
            "answer_kk_status",
            kb_answer_status,
            server_default=sa.text("'absent'"),
            nullable=False,
        ),
        sa.Column("answer_en", sa.Text(), nullable=True),
        sa.Column(
            "answer_en_status",
            kb_answer_status,
            server_default=sa.text("'absent'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("normalized_key", name="uq_kb_faq_key"),
        # An active entry must have an approved Russian answer: ru is the
        # source of truth (owner decision).
        sa.CheckConstraint(
            "NOT is_active OR answer_ru_status = 'approved'", name="active_requires_ru"
        ),
        # A status may not lie about the presence of text.
        sa.CheckConstraint(
            "(answer_ru_status = 'absent') = (answer_ru IS NULL)", name="ru_status_matches"
        ),
        sa.CheckConstraint(
            "(answer_kk_status = 'absent') = (answer_kk IS NULL)", name="kk_status_matches"
        ),
        sa.CheckConstraint(
            "(answer_en_status = 'absent') = (answer_en IS NULL)", name="en_status_matches"
        ),
    )

    op.create_table(
        "kb_faq_patterns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "faq_id",
            sa.BigInteger(),
            sa.ForeignKey("kb_faq.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("language", kb_language, nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("normalized_pattern", sa.Text(), nullable=False),
        sa.Column("token_key", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One phrasing cannot lead to two different answers — the conflict
        # surfaces on save, not as a nondeterministic answer in production.
        sa.UniqueConstraint(
            "language", "normalized_pattern", name="uq_kb_faq_patterns_normalized"
        ),
    )
    op.create_index("ix_kb_faq_patterns_faq", "kb_faq_patterns", ["faq_id"])
    op.create_index("ix_kb_faq_patterns_tokens", "kb_faq_patterns", ["language", "token_key"])

    op.create_table(
        "kb_faq_misses",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "faq_id",
            sa.BigInteger(),
            sa.ForeignKey("kb_faq.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("language", kb_language, nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_kb_faq_misses_lang", "kb_faq_misses", ["language", "created_at"])
    op.create_index("ix_kb_faq_misses_faq", "kb_faq_misses", ["faq_id"])

    op.create_table(
        "kb_processing_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_kb_processing_log_doc", "kb_processing_log", ["document_id", "created_at"]
    )
    # NOTE: kb_faq is deliberately created EMPTY — no seeded texts in code,
    # migrations or scripts (SPEC §11). The owner fills it via the admin.


def downgrade() -> None:
    op.drop_table("kb_processing_log")
    op.drop_table("kb_faq_misses")
    op.drop_table("kb_faq_patterns")
    op.drop_table("kb_faq")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.drop(bind, checkfirst=True)
