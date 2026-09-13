"""Transport outbox: every outgoing WhatsApp message goes through here.

Guarantees:
- idempotency: UNIQUE (outbound_message_id) — a retried enqueue or flush
  can never send twice;
- DRY_RUN: rows are finalized as SKIPPED_DRY_RUN and nothing leaves the
  server (acceptance mode, SPEC §17);
- bounded retries with exponential backoff + jitter, then FAILED_FINAL
  plus a system event.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import SystemEvent, WaOutbox
from app.whatsapp.provider import ProviderError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import Settings
    from app.whatsapp.provider import WhatsAppProvider

logger = structlog.get_logger(__name__)

_FLUSH_BATCH_SIZE = 10
#: wa_outbox.purpose for messages addressed to the owner, not a customer.
ALERT_PURPOSE = "alert"
#: 1, 2, 4, 8, 16 minutes: an alert survives a provider outage instead of
#: exhausting its attempts while the outage is still going on.
_ALERT_BACKOFF_BASE_SECONDS = 60
_ALERT_MAX_ATTEMPTS = 6


async def enqueue_text(
    db: AsyncSession,
    instance_pk: int,
    wa_chat_id: str,
    text: str,
    outbound_message_id: uuid.UUID,
    correlation_id: str | None = None,
    purpose: str = "customer",
) -> bool:
    """Queue a text send; returns False if this id is already queued.

    `purpose` separates the two channels that share this transport: a
    customer reply (muted by DRY_RUN) and an alert to the owner's own
    number (never muted — see services/alerts.py).
    """
    stmt = (
        pg_insert(WaOutbox)
        .values(
            instance_id=instance_pk,
            outbound_message_id=outbound_message_id,
            wa_chat_id=wa_chat_id,
            kind="text",
            body=text,
            correlation_id=correlation_id,
            purpose=purpose,
        )
        .on_conflict_do_nothing(constraint="uq_wa_outbox_outbound_message_id")
        .returning(WaOutbox.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    return inserted is not None


def _backoff(settings: Settings, retry_count: int, *, purpose: str = "customer") -> timedelta:
    base = settings.outbox_retry_base_seconds * (2**retry_count)
    if purpose == ALERT_PURPOSE:
        # An alert is not time-critical to the second, but it IS the
        # message that must eventually arrive: a customer reply is
        # pointless an hour late, while "your bot has been silent since
        # morning" is not. Minutes instead of seconds, so a provider
        # outage does not burn every attempt in six seconds.
        base = _ALERT_BACKOFF_BASE_SECONDS * (2**retry_count)
    jitter = base * (secrets.randbelow(400) / 1000)  # 0..40% jitter
    return timedelta(seconds=base + jitter)


class OutboxFlusher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: WhatsAppProvider | None,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = provider
        self._settings = settings

    async def flush_once(self) -> int:
        """Attempt due sends; returns how many rows changed state.

        ONE TRANSACTION PER MESSAGE, deliberately. The send itself is an
        HTTP call that cannot be rolled back: with a shared transaction a
        SIGKILL (or a crash) after the third of ten sends would roll back
        all three SENT marks while the customer had already received the
        messages — and the next pass would send them again. Per-row
        commits reduce that window to the single message in flight.
        """
        async with self._sessionmaker() as db:
            due = (
                (
                    await db.execute(
                        select(WaOutbox.id)
                        .where(
                            WaOutbox.state.in_(("PENDING", "FAILED_RETRYABLE")),
                            WaOutbox.next_attempt_at <= datetime.now(UTC),
                        )
                        .order_by(WaOutbox.id)
                        .limit(_FLUSH_BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
        handled = 0
        for row_id in due:
            handled += await self._flush_one(row_id)
        return handled

    async def _flush_one(self, row_id: int) -> int:
        async with self._sessionmaker() as db:
            row = (
                await db.execute(
                    select(WaOutbox)
                    .where(
                        WaOutbox.id == row_id,
                        WaOutbox.state.in_(("PENDING", "FAILED_RETRYABLE")),
                        WaOutbox.next_attempt_at <= datetime.now(UTC),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                # Taken by another pass, or no longer due.
                return 0
            try:
                await self._attempt(db, row)
            except Exception as exc:  # noqa: BLE001 - a poison row is
                # terminal, never an endless retry of the same crash.
                logger.exception("outbox_unexpected_error", outbox_id=row.id)
                self._fail(db, row, f"unexpected:{type(exc).__name__}", retryable=False)
            await db.commit()
            return 1

    async def _attempt(self, db: AsyncSession, row: WaOutbox) -> None:
        log = logger.bind(
            outbox_id=row.id,
            outbound_message_id=str(row.outbound_message_id),
            correlation_id=row.correlation_id,
        )
        if self._settings.dry_run and row.purpose != ALERT_PURPOSE:
            # Acceptance mode: fully processed, deliberately not sent.
            # Alerts are exempt on purpose — acceptance is exactly when
            # the owner must still hear "the budget is gone, the bot is
            # quiet", and that message goes to their own number, not to
            # a customer.
            row.state = "SKIPPED_DRY_RUN"
            row.sent_at = datetime.now(UTC)
            log.info("outbox_dry_run_skip")
            return
        if self._provider is None:
            self._fail(db, row, "provider_not_configured", retryable=False)
            return
        try:
            if row.kind == "file" and row.file_ref:
                result = await self._provider.send_file(
                    row.wa_chat_id, row.file_ref, row.file_ref.rsplit("/", 1)[-1], row.body
                )
            else:
                result = await self._provider.send_text(row.wa_chat_id, row.body or "")
        except ProviderError as exc:
            self._fail(db, row, f"provider:{exc.status_code}", retryable=exc.retryable)
            return
        row.state = "SENT"
        row.provider_message_id = result.provider_message_id
        row.sent_at = datetime.now(UTC)
        row.error_code = None
        row.error_detail = None
        log.info("outbox_sent", provider_message_id=result.provider_message_id)

    def _fail(self, db: AsyncSession, row: WaOutbox, code: str, *, retryable: bool) -> None:
        row.retry_count += 1
        row.error_code = code
        max_attempts = (
            _ALERT_MAX_ATTEMPTS
            if row.purpose == ALERT_PURPOSE
            else self._settings.retry_max_attempts
        )
        exhausted = row.retry_count >= max_attempts
        if retryable and not exhausted:
            row.state = "FAILED_RETRYABLE"
            row.next_attempt_at = datetime.now(UTC) + _backoff(
                self._settings, row.retry_count, purpose=row.purpose
            )
            logger.warning(
                "outbox_retry_scheduled",
                outbox_id=row.id,
                retry_count=row.retry_count,
                error_code=code,
            )
            return
        row.state = "FAILED_FINAL"
        db.add(
            SystemEvent(
                level="error",
                component="outbox",
                event="send_failed_final",
                detail={"outbox_id": row.id, "error_code": code},
                correlation_id=row.correlation_id,
            )
        )
        logger.error("outbox_failed_final", outbox_id=row.id, error_code=code)
