"""Media (phase 7): media_jobs work queue + a DB barrier against paying twice

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-08

Two things here exist purely to protect money and messages:

1. `media_jobs` — one row per media message (UNIQUE message_id), with its
   OWN attempt counter and lease. Reusing messages.retry_count/
   next_attempt_at for this would collide with the AI pipeline: a voice
   note that spent two attempts on transcription would arrive at the chat
   call already out of retries, and the reservation sub-correlation is
   keyed by that same counter.
2. A partial UNIQUE on `ai_calls (correlation_id, call_type)` — the
   database barrier required by SPEC invariant 2 (dedup by constraint,
   not by process memory). The transcription/vision row is inserted
   BEFORE the paid call, so a crash mid-call cannot lead to a second
   charge for the same file: the retry hits the constraint and reuses the
   existing row instead of paying again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

media_type = postgresql.ENUM(
    "voice",
    "image",
    "video",
    "gif",
    "sticker",
    "document",
    "other",
    name="media_type",
    create_type=False,
)
media_job_state = postgresql.ENUM(
    "pending",
    "processing",
    "done",
    "refused",
    "failed",
    name="media_job_state",
    # Created explicitly in upgrade(); letting create_table create it too
    # would raise DuplicateObject.
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    media_job_state.create(bind, checkfirst=True)

    op.create_table(
        "media_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_type", media_type, nullable=False),
        sa.Column(
            "state", media_job_state, server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # Held by whoever is working the job right now; a crashed worker's
        # lease simply expires and the job becomes claimable again.
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        # The provider's signed download URL lives HERE, never in
        # messages.media_ref (which holds our own path only, SPEC §13).
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("file_bytes", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 2), nullable=True),
        # Filled once the bytes are known: a re-sent identical file can
        # reuse the existing transcript for free (logic lands later).
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # One job per message: a redelivered notification or a double
        # enqueue can never create a second paid unit of work.
        sa.UniqueConstraint("message_id", name="uq_media_jobs_message"),
    )
    # The worker's pickup query: only unfinished jobs, newest last.
    op.create_index(
        "ix_media_jobs_claimable",
        "media_jobs",
        ["next_attempt_at"],
        postgresql_where=sa.text("state IN ('pending', 'processing')"),
    )
    # The pipeline's "does this chat still owe media work?" check, which
    # keeps replies in order and keeps the worker loop from spinning.
    op.create_index(
        "ix_media_jobs_chat_open",
        "media_jobs",
        ["chat_id"],
        postgresql_where=sa.text("state IN ('pending', 'processing')"),
    )
    op.create_index(
        "ix_media_jobs_sha256", "media_jobs", ["sha256"], postgresql_where=sa.text("sha256 IS NOT NULL")
    )

    # THE money barrier: at most one paid call of a given type per
    # correlation. Partial, because chat calls legitimately repeat per
    # retry attempt under different correlations and NULLs must not clash.
    op.create_index(
        "uq_ai_calls_correlation_type",
        "ai_calls",
        ["correlation_id", "call_type"],
        unique=True,
        postgresql_where=sa.text("call_type IN ('transcription', 'vision')"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_calls_correlation_type", table_name="ai_calls")
    op.drop_index("ix_media_jobs_sha256", table_name="media_jobs")
    op.drop_index("ix_media_jobs_chat_open", table_name="media_jobs")
    op.drop_index("ix_media_jobs_claimable", table_name="media_jobs")
    op.drop_table("media_jobs")
    media_job_state.drop(op.get_bind(), checkfirst=True)
