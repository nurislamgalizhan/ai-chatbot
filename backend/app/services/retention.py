"""Scheduled cleanup of everything that grows forever.

The policy itself is docs/security-and-cost-controls.md §5; this module
is its only implementation. Three rules shape the code:

1. **Nothing the owner paid for or needs as history is deleted.** Chats,
   messages and ai_calls stay forever: history is the product and
   ai_calls is the money record. What goes is transport debris, expired
   auth state and technical journals past their window.
2. **Deletes are batched by primary key**, never one giant statement: a
   single `DELETE ... WHERE created_at < x` over half a million rows
   holds locks long enough to stall the pipeline on a 2-vCPU VPS.
3. **Every window is a setting**, so the owner can shorten retention of
   personal data without a code change (SPEC §14: customer texts live in
   raw_payload, and that is the one wipe that is about privacy rather
   than disk).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import ColumnElement, delete, null, select, update

from app.models import (
    AdminSession,
    AlertNotification,
    AuditLog,
    BudgetReservation,
    InboundNotification,
    KbFaqMiss,
    LoginChallenge,
    MediaJob,
    SemanticCacheEntry,
    SystemEvent,
    WaOutbox,
)
from app.models.enums import MediaJobState
from app.services import app_settings
from app.services.app_settings import get_setting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

#: Rows per statement. Small enough that a batch never holds locks for
#: long, large enough that a year of debris drains in minutes.
BATCH = 500
#: Safety stop: one pass cleans at most this many rows per policy, so a
#: first run on a huge table cannot monopolise the worker.
MAX_BATCHES_PER_PASS = 40

#: Transport rows are debris the moment they are terminal, but keeping a
#: month of them makes "why did this message not arrive" answerable.
TERMINAL_OUTBOX_STATES = ("SENT", "SKIPPED_DRY_RUN", "FAILED_FINAL")
TERMINAL_MEDIA_STATES = (MediaJobState.DONE, MediaJobState.REFUSED, MediaJobState.FAILED)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What one pass actually removed, per policy."""

    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def _delete_batched(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    model: Any,
    where: ColumnElement[bool],
) -> int:
    """Delete matching rows in primary-key batches; returns the count."""
    removed = 0
    for _ in range(MAX_BATCHES_PER_PASS):
        async with sessionmaker() as db:
            ids = (
                (await db.execute(select(model.id).where(where).order_by(model.id).limit(BATCH)))
                .scalars()
                .all()
            )
            if not ids:
                return removed
            result = await db.execute(delete(model).where(model.id.in_(ids)))
            await db.commit()
        removed += int(getattr(result, "rowcount", 0) or 0)
        if len(ids) < BATCH:
            return removed
    return removed


async def wipe_raw_payloads(
    sessionmaker: async_sessionmaker[AsyncSession], cutoff: datetime
) -> int:
    """Null out the provider payload of processed notifications.

    The ROW stays: it is the deduplication barrier (UNIQUE instance_id +
    provider_message_id, SPEC invariant 2) and dropping it would let a
    redelivered notification produce a second paid answer years later.
    What goes is the customer's text, name and metadata inside the JSONB
    — the copy nobody reads after the message exists in `messages`.
    """
    wiped = 0
    for _ in range(MAX_BATCHES_PER_PASS):
        async with sessionmaker() as db:
            ids = (
                (
                    await db.execute(
                        select(InboundNotification.id)
                        .where(
                            InboundNotification.raw_payload.is_not(None),
                            InboundNotification.received_at < cutoff,
                            # An unprocessed row still needs its payload:
                            # wiping it would turn a notification that is
                            # merely stuck into a message lost for good.
                            InboundNotification.status != "stored",
                        )
                        .order_by(InboundNotification.id)
                        .limit(BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return wiped
            await db.execute(
                update(InboundNotification)
                .where(InboundNotification.id.in_(ids))
                # null(), not None: SQLAlchemy writes Python None into a
                # JSONB column as the JSON value `null`, which is NOT
                # SQL NULL. The row would keep matching
                # `raw_payload IS NOT NULL` and every pass would rewrite
                # the same rows forever.
                .values(raw_payload=null())
            )
            await db.commit()
        wiped += len(ids)
        if len(ids) < BATCH:
            return wiped
    return wiped


async def wipe_media_download_urls(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Drop the provider's signed file URL from finished jobs.

    It points at the customer's own voice note or photo on the provider's
    servers. Once the job is terminal nothing uses it, so keeping it is
    only a way to leak a customer's file through a database dump.
    """
    cleared = 0
    for _ in range(MAX_BATCHES_PER_PASS):
        async with sessionmaker() as db:
            ids = (
                (
                    await db.execute(
                        select(MediaJob.id)
                        .where(
                            MediaJob.download_url.is_not(None),
                            MediaJob.state.in_(TERMINAL_MEDIA_STATES),
                        )
                        .order_by(MediaJob.id)
                        .limit(BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return cleared
            await db.execute(update(MediaJob).where(MediaJob.id.in_(ids)).values(download_url=None))
            await db.commit()
        cleared += len(ids)
        if len(ids) < BATCH:
            return cleared
    return cleared


async def run_retention(sessionmaker: async_sessionmaker[AsyncSession]) -> RetentionResult:
    """One full pass of every policy. Safe to run as often as you like:
    each policy is idempotent and stops when there is nothing left."""
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        raw_days = int(await get_setting(db, app_settings.RETENTION_RAW_PAYLOAD_DAYS))
        events_days = int(await get_setting(db, app_settings.RETENTION_SYSTEM_EVENTS_DAYS))
        misses_days = int(await get_setting(db, app_settings.RETENTION_FAQ_MISSES_DAYS))
        audit_days = int(await get_setting(db, app_settings.RETENTION_AUDIT_LOG_DAYS))
        transport_days = int(await get_setting(db, app_settings.RETENTION_TRANSPORT_DAYS))

    counts: dict[str, int] = {}

    counts["inbound_payloads"] = await wipe_raw_payloads(
        sessionmaker, now - timedelta(days=raw_days)
    )
    counts["media_download_urls"] = await wipe_media_download_urls(sessionmaker)

    counts["system_events"] = await _delete_batched(
        sessionmaker,
        model=SystemEvent,
        where=(SystemEvent.created_at < now - timedelta(days=events_days))
        # The backup rows are not journal entries, they are the ONLY
        # record of when a dump last succeeded: «Система» reads them and
        # the stale-backup alert is computed from them. Deleting them
        # would make the alert go quiet exactly when backups have been
        # failing for longer than the retention window.
        & (SystemEvent.component != "backup"),
    )
    counts["backup_events"] = await _delete_batched(
        sessionmaker,
        model=SystemEvent,
        # Keep a year of backup history: enough to see "it stopped in
        # March", small enough to never matter (one row per day).
        where=(SystemEvent.component == "backup")
        & (SystemEvent.created_at < now - timedelta(days=365)),
    )
    counts["kb_faq_misses"] = await _delete_batched(
        sessionmaker,
        model=KbFaqMiss,
        where=KbFaqMiss.created_at < now - timedelta(days=misses_days),
    )
    counts["audit_log"] = await _delete_batched(
        sessionmaker,
        model=AuditLog,
        where=AuditLog.created_at < now - timedelta(days=audit_days),
    )
    # Transport debris: the domain `messages` row is the history, these
    # are the delivery attempts behind it.
    counts["wa_outbox"] = await _delete_batched(
        sessionmaker,
        model=WaOutbox,
        where=(WaOutbox.state.in_(TERMINAL_OUTBOX_STATES))
        & (WaOutbox.created_at < now - timedelta(days=transport_days)),
    )
    counts["media_jobs"] = await _delete_batched(
        sessionmaker,
        model=MediaJob,
        where=(MediaJob.state.in_(TERMINAL_MEDIA_STATES))
        & (MediaJob.created_at < now - timedelta(days=transport_days)),
    )
    # Auth state: an expired session or challenge grants nothing, and
    # keeping the IP/user-agent of a login from last spring is data we
    # have no reason to hold.
    counts["admin_sessions"] = await _delete_batched(
        sessionmaker,
        model=AdminSession,
        where=AdminSession.expires_at < now - timedelta(days=30),
    )
    counts["login_challenges"] = await _delete_batched(
        sessionmaker,
        model=LoginChallenge,
        where=LoginChallenge.expires_at < now - timedelta(days=1),
    )
    counts["semantic_cache"] = await _delete_batched(
        sessionmaker,
        model=SemanticCacheEntry,
        where=SemanticCacheEntry.expires_at < now,
    )
    # Budget bookkeeping: the ledger (the money) stays; these are the
    # per-call holds that were settled or released long ago.
    counts["budget_reservations"] = await _delete_batched(
        sessionmaker,
        model=BudgetReservation,
        where=(BudgetReservation.status != "held")
        & (BudgetReservation.created_at < now - timedelta(days=transport_days)),
    )
    counts["alert_notifications"] = await _delete_batched(
        sessionmaker,
        model=AlertNotification,
        where=(AlertNotification.status.in_(("sent", "skipped")))
        & (AlertNotification.created_at < now - timedelta(days=events_days)),
    )

    result = RetentionResult(counts={k: v for k, v in counts.items() if v})
    if result.total:
        logger.info("retention_pass", **result.counts)
    return result
