"""Acceptance review: a pre-payment barrier for chat calls, month periods

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15

1. `ai_calls.barrier_key` + partial UNIQUE index. The media path has had
   a pre-payment barrier since phase 7 (a `started` row keyed on the
   correlation, guarded by a partial UNIQUE index for transcription and
   vision). The chat path had none: a worker killed between the model's
   answer and the accounting row left no trace, the sweeper re-queued
   the message and the same question was paid for twice. The chat call
   now claims `{correlation}:main` here BEFORE it is made.

2. Month budget periods stored their `period_end` as the first day of
   the NEXT month while every reader compares `period_end >= today`
   inclusively — so on the 1st of a month two month rows matched and
   the closed month could be shown (and re-limited) as the current one.
   New rows store the last day of the month; existing rows are fixed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_calls", sa.Column("barrier_key", sa.Text(), nullable=True))
    op.create_index(
        "uq_ai_calls_barrier_key",
        "ai_calls",
        ["barrier_key"],
        unique=True,
        postgresql_where=sa.text("barrier_key IS NOT NULL"),
    )
    op.execute(
        "UPDATE budget_ledger SET period_end = period_end - 1 "
        "WHERE scope = 'global_month' AND period_end > period_start + 27"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE budget_ledger SET period_end = period_end + 1 "
        "WHERE scope = 'global_month' AND period_end < period_start + 27"
    )
    op.drop_index("uq_ai_calls_barrier_key", table_name="ai_calls")
    op.drop_column("ai_calls", "barrier_key")
