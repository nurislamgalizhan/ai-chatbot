"""AI and money: agent instructions, ai_calls, prices, fx, budget, cache

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06

model_prices, fx_rates and agent_instructions are created EMPTY: prices
are seeded by an explicit owner-run command (make seed-prices), never
silently by a migration; the instruction is authored in the admin UI.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

ai_call_type = postgresql.ENUM(
    "chat", "transcription", "embedding", "vision", name="ai_call_type", create_type=False
)
price_unit = postgresql.ENUM(
    "per_1m_input",
    "per_1m_cached_input",
    "per_1m_output",
    "per_minute_audio",
    name="price_unit",
    create_type=False,
)
budget_scope = postgresql.ENUM(
    "global_day", "global_month", "chat", "user_day", name="budget_scope", create_type=False
)
reservation_status = postgresql.ENUM(
    "held", "settled", "released", name="reservation_status", create_type=False
)
kb_language = postgresql.ENUM("ru", "kk", "en", name="kb_language", create_type=False)

_NEW_ENUMS = (ai_call_type, price_unit, budget_scope, reservation_status)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _NEW_ENUMS:
        enum.create(bind, checkfirst=True)

    # Terminal state for an answered INBOUND customer message; SENT stays
    # the terminal state of outbound messages. Safe inside the migration
    # transaction because no row is written with it here.
    op.execute("ALTER TYPE msg_state ADD VALUE IF NOT EXISTS 'ANSWERED'")

    # Retry scheduling for the AI pipeline (backoff without hot-looping).
    op.add_column(
        "messages", sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )

    op.create_table(
        "agent_instructions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("style", sa.Text(), nullable=True),
        sa.Column("allowed_topics", postgresql.JSONB(), nullable=True),
        sa.Column("forbidden_topics", postgresql.JSONB(), nullable=True),
        sa.Column(
            "fallback_templates",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("escalation_rules", postgresql.JSONB(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("reasoning_effort", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "author_user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("version", name="uq_agent_instructions_version"),
    )
    # Exactly one active version; a race of two activations is resolved
    # by the database, not by application luck.
    op.create_index(
        "uq_agent_instructions_active",
        "agent_instructions",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "ai_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("call_type", ai_call_type, nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cached_input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 8), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_kzt", sa.Numeric(12, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("rag_chunk_ids", postgresql.JSONB(), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reasoning_effort", sa.Text(), nullable=True),
        sa.Column("instruction_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_ai_calls_created", "ai_calls", ["created_at"])
    op.create_index("ix_ai_calls_chat", "ai_calls", ["chat_id"])
    op.create_index("ix_ai_calls_message", "ai_calls", ["message_id"])
    op.create_index("ix_ai_calls_correlation", "ai_calls", ["correlation_id"])
    op.create_index("ix_ai_calls_model_created", "ai_calls", ["model", "created_at"])

    op.create_table(
        "model_prices",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("unit", price_unit, nullable=False),
        sa.Column("price_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One unambiguous price per moment; costing takes the latest
        # effective_from <= call time.
        sa.UniqueConstraint("model", "unit", "effective_from", name="uq_model_prices_effective"),
    )

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("pair", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(12, 4), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "changed_by",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("pair", "effective_from", name="uq_fx_rates_effective"),
    )

    op.create_table(
        "budget_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("scope", budget_scope, nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("spent_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("limit_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The heart of spend control: one counter row per scope+period,
        # all concurrent reservations serialize on it inside PostgreSQL.
        sa.UniqueConstraint("scope", "scope_key", "period_start", name="uq_budget_ledger_scope"),
        # Money never goes negative; a released reservation cannot
        # "refund" more than was held.
        sa.CheckConstraint(
            "reserved_usd >= 0 AND spent_usd >= 0", name="budget_ledger_non_negative"
        ),
    )

    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "ledger_id",
            sa.BigInteger(),
            sa.ForeignKey("budget_ledger.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "status", reservation_status, server_default=sa.text("'held'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Idempotent settle/release: a retried operation cannot move the
        # same money twice.
        sa.UniqueConstraint(
            "correlation_id", "ledger_id", name="uq_budget_reservations_correlation"
        ),
    )
    # The sweeper's working set: stuck holds older than N minutes.
    op.create_index(
        "ix_budget_reservations_held",
        "budget_reservations",
        ["status", "created_at"],
        postgresql_where=sa.text("status = 'held'"),
    )

    op.create_table(
        "semantic_cache",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("normalized_query_hash", sa.Text(), nullable=False),
        sa.Column("language", kb_language, nullable=False),
        sa.Column("category", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("kb_version", sa.Integer(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # Deterministic cache key; kb_version in the key = invalidation on
        # any KB change without scanning the cache.
        sa.UniqueConstraint(
            "normalized_query_hash",
            "language",
            "category",
            "kb_version",
            name="uq_semantic_cache_key",
        ),
    )
    op.create_index("ix_semantic_cache_expires", "semantic_cache", ["expires_at"])

    op.create_table(
        "alert_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("period_key", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # "Once per threshold, no spam": re-checking the same threshold in
        # the same period cannot produce a second alert.
        sa.UniqueConstraint(
            "kind", "event_key", "period_key", name="uq_alert_notifications_dedupe"
        ),
    )


def downgrade() -> None:
    op.drop_table("alert_notifications")
    op.drop_table("semantic_cache")
    op.drop_table("budget_reservations")
    op.drop_table("budget_ledger")
    op.drop_table("fx_rates")
    op.drop_table("model_prices")
    op.drop_table("ai_calls")
    op.drop_table("agent_instructions")
    op.drop_column("messages", "next_attempt_at")
    # The ANSWERED enum value stays: PostgreSQL cannot drop enum values.
    bind = op.get_bind()
    for enum in _NEW_ENUMS:
        enum.drop(bind, checkfirst=True)
