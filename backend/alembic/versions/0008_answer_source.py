"""Record HOW each answer was produced, so free answers can be counted

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-12

Until now a free answer left no trace anywhere. An FAQ hit and a cache
hit return before any `ai_calls` row is written — correctly, since
nothing was paid — but that also made them uncountable: "how many
answers cost nothing" and "cache hit rate" had no numerator AND no
denominator. `ai_calls.cache_hit` existed as a column and was never
written to.

One nullable column on `messages` fixes both, filled from the outcome
the pipeline already has. It is the difference between reporting a real
number and drawing a plausible one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

answer_source = postgresql.ENUM(
    "model",
    "faq",
    "cache",
    "service",
    name="answer_source",
    create_type=False,
)


def upgrade() -> None:
    answer_source.create(op.get_bind(), checkfirst=True)
    op.add_column("messages", sa.Column("answer_source", answer_source, nullable=True))
    # Reports group outgoing AI answers by day and source.
    op.create_index(
        "ix_messages_answer_source",
        "messages",
        ["created_at"],
        postgresql_where=sa.text("answer_source IS NOT NULL"),
    )
    # Spend by day and by dialog: the two axes every money report uses.
    # ix_ai_calls_chat is (chat_id) alone, which still scans a dialog's
    # whole history to answer "this month".
    op.create_index("ix_ai_calls_chat_created", "ai_calls", ["chat_id", "created_at"])
    # ix_ai_calls_model_created is (model, created_at) — the wrong order
    # for "break the last 30 days down by model".
    op.create_index("ix_ai_calls_created_model", "ai_calls", ["created_at", "model"])
    # The errors panel (§12.1) and "paid but produced nothing".
    op.create_index(
        "ix_ai_calls_failed",
        "ai_calls",
        ["created_at"],
        postgresql_where=sa.text("status <> 'ok'"),
    )


def downgrade() -> None:
    op.drop_index("ix_ai_calls_failed", table_name="ai_calls")
    op.drop_index("ix_ai_calls_created_model", table_name="ai_calls")
    op.drop_index("ix_ai_calls_chat_created", table_name="ai_calls")
    op.drop_index("ix_messages_answer_source", table_name="messages")
    op.drop_column("messages", "answer_source")
    answer_source.drop(op.get_bind(), checkfirst=True)
