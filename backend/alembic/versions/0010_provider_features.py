"""Quoting the customer's message, and the provider settings we watch

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14

Two columns, both from reading the provider's documentation properly:

1. `wa_outbox.quoted_message_id` — `sendMessage` accepts
   `quotedMessageId`, and quoting matters here: a manager answers from
   the phone in the same chat as the bot, so an unquoted answer can land
   under someone else's line and read as a reply to it.

2. `whatsapp_instances.settings_json` / `settings_checked_at` — the
   instance's own configuration decides whether this bot can work at
   all. With a webhook URL configured, the provider delivers
   notifications THERE and our polling queue stays empty forever: no
   error, no exception, the bot simply answers nobody. Storing the last
   read of those settings is what lets «Система» say so out loud.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wa_outbox", sa.Column("quoted_message_id", sa.Text(), nullable=True))
    op.add_column(
        "whatsapp_instances",
        sa.Column("settings_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "whatsapp_instances",
        sa.Column("settings_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_instances", "settings_checked_at")
    op.drop_column("whatsapp_instances", "settings_json")
    op.drop_column("wa_outbox", "quoted_message_id")
