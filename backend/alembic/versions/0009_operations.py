"""Alert delivery and the indexes retention needs to stay cheap

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-14

Two unrelated-looking things, both from phase 9 (running this unattended
for a year):

1. `alert_notifications` gained a text, an attempt counter, a schedule
   and a last error. Until now the table was pure dedup memory — rows
   were written and nothing ever read them, so a spent budget or a dead
   WhatsApp connection was visible only to someone who went looking.

2. Retention indexes. Every cleanup added in this phase deletes by a
   timestamp; without an index each nightly pass is a sequential scan of
   the biggest tables on a 2-vCPU VPS shared with the owner's other
   sites. The partial index on `inbound_notifications` covers only rows
   that still hold a payload, which is exactly the set being wiped and a
   small fraction of the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- alert delivery ---------------------------------------------------
    # Alerts travel down the SAME transport as customer messages (one
    # idempotency barrier, one retry policy, one place that can send),
    # but they are a different channel: DRY_RUN mutes the customer one
    # and must not mute the "your bot went quiet" one.
    op.add_column(
        "wa_outbox",
        sa.Column("purpose", sa.Text(), server_default=sa.text("'customer'"), nullable=False),
    )
    op.add_column(
        "alert_notifications",
        sa.Column("outbound_message_id", sa.Uuid(), nullable=True),
    )
    # The provider's own view of the instance ("authorized" or not),
    # refreshed by the owner's button, not by rendering a page.
    op.add_column("whatsapp_instances", sa.Column("state", sa.Text(), nullable=True))
    op.add_column(
        "whatsapp_instances",
        sa.Column("state_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Retry budget for notifications whose processing hit a transient
    # database error: without it, one deadlock threw away a customer's
    # message permanently.
    op.add_column(
        "inbound_notifications",
        sa.Column("process_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("alert_notifications", sa.Column("body", sa.Text(), nullable=True))
    op.add_column(
        "alert_notifications",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "alert_notifications",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.add_column("alert_notifications", sa.Column("last_error", sa.Text(), nullable=True))
    # The sender polls exactly this predicate every few seconds.
    op.create_index(
        "ix_alert_notifications_pending",
        "alert_notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_alert_notifications_created", "alert_notifications", ["created_at"])

    # --- retention indexes ------------------------------------------------
    op.create_index(
        "ix_inbound_notifications_payload_age",
        "inbound_notifications",
        ["received_at"],
        postgresql_where=sa.text("raw_payload IS NOT NULL"),
    )
    op.create_index("ix_wa_outbox_created", "wa_outbox", ["created_at"])
    # The hourly media sweep looked for old files with a sequential scan
    # of `messages` — the largest table in the system, every hour,
    # forever. Partial: only rows that still have a file at all.
    op.create_index(
        "ix_messages_media_retention",
        "messages",
        ["created_at"],
        postgresql_where=sa.text("media_ref IS NOT NULL"),
    )
    op.create_index("ix_media_jobs_created", "media_jobs", ["created_at"])
    op.create_index("ix_kb_faq_misses_created", "kb_faq_misses", ["created_at"])
    op.create_index("ix_admin_sessions_expires", "admin_sessions", ["expires_at"])
    op.create_index("ix_login_challenges_expires", "login_challenges", ["expires_at"])
    op.create_index("ix_budget_reservations_created", "budget_reservations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_budget_reservations_created", table_name="budget_reservations")
    op.drop_index("ix_login_challenges_expires", table_name="login_challenges")
    op.drop_index("ix_admin_sessions_expires", table_name="admin_sessions")
    op.drop_index("ix_kb_faq_misses_created", table_name="kb_faq_misses")
    op.drop_index("ix_media_jobs_created", table_name="media_jobs")
    op.drop_index("ix_messages_media_retention", table_name="messages")
    op.drop_index("ix_wa_outbox_created", table_name="wa_outbox")
    op.drop_index("ix_inbound_notifications_payload_age", table_name="inbound_notifications")
    op.drop_index("ix_alert_notifications_created", table_name="alert_notifications")
    op.drop_index("ix_alert_notifications_pending", table_name="alert_notifications")
    op.drop_column("inbound_notifications", "process_attempts")
    op.drop_column("whatsapp_instances", "state_checked_at")
    op.drop_column("whatsapp_instances", "state")
    op.drop_column("alert_notifications", "outbound_message_id")
    op.drop_column("wa_outbox", "purpose")
    op.drop_column("alert_notifications", "last_error")
    op.drop_column("alert_notifications", "next_attempt_at")
    op.drop_column("alert_notifications", "attempts")
    op.drop_column("alert_notifications", "body")
