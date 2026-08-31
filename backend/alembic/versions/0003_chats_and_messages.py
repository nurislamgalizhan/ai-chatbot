"""chats, messages, chat memory, notes, tags

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

chat_mode = postgresql.ENUM(
    "AUTO_AI", "HUMAN", "PAUSED_UNTIL", "DISABLED", name="chat_mode", create_type=False
)
last_actor = postgresql.ENUM("ai", "human", "customer", name="last_actor", create_type=False)
attention_reason = postgresql.ENUM(
    "handoff", "processing_error", "blocked_by_limit", "manual",
    name="attention_reason",
    create_type=False,
)
msg_direction = postgresql.ENUM("in", "out", name="msg_direction", create_type=False)
msg_source = postgresql.ENUM(
    "customer", "ai", "operator_panel", "operator_phone", "system",
    name="msg_source",
    create_type=False,
)
media_type = postgresql.ENUM(
    "voice", "image", "video", "gif", "sticker", "document", "other",
    name="media_type",
    create_type=False,
)
msg_state = postgresql.ENUM(
    "RECEIVED",
    "QUEUED",
    "PROCESSING",
    "GENERATED",
    "SENT",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "IGNORED",
    "HANDED_TO_HUMAN",
    "BLOCKED_BY_LIMIT",
    name="msg_state",
    create_type=False,
)

_ENUMS = (
    chat_mode,
    last_actor,
    attention_reason,
    msg_direction,
    msg_source,
    media_type,
    msg_state,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("wa_chat_id", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("mode", chat_mode, server_default=sa.text("'AUTO_AI'"), nullable=False),
        sa.Column("paused_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("mode_changed_by", sa.Text(), nullable=True),
        sa.Column("mode_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("mode_change_reason", sa.Text(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("unread_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_message_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_actor", last_actor, nullable=True),
        sa.Column(
            "total_cost_usd", sa.Numeric(12, 6), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "needs_attention", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("attention_reason", attention_reason, nullable=True),
        sa.Column("attention_set_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attention_cleared_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attention_cleared_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One WhatsApp counterpart = exactly one chat; a race between two
        # concurrent notifications resolves on the constraint, not in code.
        sa.UniqueConstraint("instance_id", "wa_chat_id", name="uq_chats_wa_chat"),
    )
    # Chat list ordering / cursor pagination.
    op.create_index(
        "ix_chats_last_message", "chats", [sa.text("last_message_at DESC"), sa.text("id DESC")]
    )
    # "Needs attention" filter without scanning the archive.
    op.create_index(
        "ix_chats_needs_attention",
        "chats",
        ["needs_attention"],
        postgresql_where=sa.text("needs_attention"),
    )
    # Scheduler lookup for paused chats (used from phase 4).
    op.create_index(
        "ix_chats_paused",
        "chats",
        ["paused_until"],
        postgresql_where=sa.text("mode = 'PAUSED_UNTIL'"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", msg_direction, nullable=False),
        sa.Column("source", msg_source, nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("outbound_message_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_type", media_type, nullable=True),
        sa.Column("media_ref", sa.Text(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("state", msg_state, nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("delivery_status", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # A redelivered/reprocessed inbound message may not become a second row.
    op.create_index(
        "uq_messages_chat_provider",
        "messages",
        ["chat_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    # Idempotent panel/AI sends (retry of the same client_message_id).
    op.create_index(
        "uq_messages_outbound_id",
        "messages",
        ["outbound_message_id"],
        unique=True,
        postgresql_where=sa.text("outbound_message_id IS NOT NULL"),
    )
    # Chat history with cursor pagination.
    op.create_index("ix_messages_chat", "messages", ["chat_id", "id"])
    # Worker pickup / reconciliation; partial — terminal states dominate.
    op.create_index(
        "ix_messages_active_states",
        "messages",
        ["state"],
        postgresql_where=sa.text(
            "state IN ('QUEUED', 'PROCESSING', 'GENERATED', 'FAILED_RETRYABLE')"
        ),
    )

    op.create_table(
        "chat_memory",
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("interest", sa.Text(), nullable=True),
        sa.Column("requested_date", sa.Text(), nullable=True),
        sa.Column("requested_time", sa.Text(), nullable=True),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column(
            "handoff_required", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("last_topic", sa.Text(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("summary_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "internal_notes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_internal_notes_chat", "internal_notes", ["chat_id", "created_at"])

    op.create_table(
        "tags",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # "Аренда" and "аренда" must not become two different tags.
    op.create_index("uq_tags_name_lower", "tags", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "chat_tags",
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.BigInteger(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_chat_tags_tag", "chat_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_table("chat_tags")
    op.drop_table("tags")
    op.drop_table("internal_notes")
    op.drop_table("chat_memory")
    op.drop_table("messages")
    op.drop_table("chats")
    bind = op.get_bind()
    for enum in _ENUMS:
        enum.drop(bind, checkfirst=True)
