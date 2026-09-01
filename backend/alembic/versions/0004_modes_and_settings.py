"""chat mode history and settings

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

chat_mode = postgresql.ENUM(
    "AUTO_AI", "HUMAN", "PAUSED_UNTIL", "DISABLED", name="chat_mode", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "chat_mode_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "chat_id",
            sa.BigInteger(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_mode", chat_mode, nullable=False),
        sa.Column("to_mode", chat_mode, nullable=False),
        sa.Column(
            "actor_user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("paused_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chat_mode_history_chat", "chat_mode_history", ["chat_id", "created_at"]
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("value_type", sa.Text(), nullable=True),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # No seed rows: defaults live in code; the table stores explicit
    # owner-made changes only.


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("chat_mode_history")
