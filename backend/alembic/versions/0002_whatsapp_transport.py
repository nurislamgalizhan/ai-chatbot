"""whatsapp transport: instances, inbox, outbox, system events

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_instances",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("token_ref", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_polled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The same provider instance registered twice would double-poll and
        # steal each other's notifications via DeleteNotification.
        sa.UniqueConstraint("provider", "instance_id", name="uq_whatsapp_instances_provider"),
    )

    op.create_table(
        "inbound_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.Text(), nullable=False),
        sa.Column("raw_payload", JSONB(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'stored'"), nullable=False),
        # THE deduplication barrier: one notification = at most one row =
        # at most one downstream processing (SPEC invariant 3).
        sa.UniqueConstraint(
            "instance_id", "provider_message_id", name="uq_inbound_notifications_dedupe"
        ),
    )
    # Pipeline pickup and retention cleanup scans.
    op.create_index(
        "ix_inbound_notifications_status",
        "inbound_notifications",
        ["status", "received_at"],
    )

    op.create_table(
        "wa_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("outbound_message_id", sa.Uuid(), nullable=False),
        sa.Column("wa_chat_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'text'"), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("file_ref", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Idempotent sending: a retried enqueue/flush can never duplicate.
        sa.UniqueConstraint("outbound_message_id", name="uq_wa_outbox_outbound_message_id"),
    )
    # Flusher work queue: only rows still awaiting a send attempt.
    op.create_index(
        "ix_wa_outbox_pending",
        "wa_outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("state IN ('PENDING', 'FAILED_RETRYABLE')"),
    )

    op.create_table(
        "system_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_system_events_created_at", "system_events", ["created_at"])
    op.create_index("ix_system_events_level", "system_events", ["level", "created_at"])


def downgrade() -> None:
    op.drop_table("system_events")
    op.drop_table("wa_outbox")
    op.drop_table("inbound_notifications")
    op.drop_table("whatsapp_instances")
