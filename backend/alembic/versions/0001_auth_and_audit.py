"""auth and audit tables

Revision ID: 0001
Revises:
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector is required from phase 5; enabling it here keeps every
    # environment (dev, test, prod) uniform from the first migration.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "admin_user",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("totp_secret_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
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
        sa.UniqueConstraint("phone", name="uq_admin_user_phone"),
    )

    op.create_table(
        "admin_totp_recovery_codes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Partial: login only ever scans unused codes.
    op.create_index(
        "ix_recovery_codes_user_unused",
        "admin_totp_recovery_codes",
        ["user_id"],
        postgresql_where=sa.text("used_at IS NULL"),
    )

    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_token_hash", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # O(1) lookup by cookie token hash; uniqueness rejects collisions.
        sa.UniqueConstraint(
            "session_token_hash", name="uq_admin_sessions_session_token_hash"
        ),
    )
    # "revoke all" / expiry cleanup scan.
    op.create_index("ix_admin_sessions_user_expiry", "admin_sessions", ["user_id", "expires_at"])

    op.create_table(
        "login_challenges",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("token_hash", name="uq_login_challenges_token_hash"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("before", JSONB(), nullable=True),
        sa.Column("after", JSONB(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("login_challenges")
    op.drop_table("admin_sessions")
    op.drop_table("admin_totp_recovery_codes")
    op.drop_table("admin_user")
    # The vector extension is intentionally kept: it may be shared and
    # dropping extensions on downgrade is riskier than leaving them.
