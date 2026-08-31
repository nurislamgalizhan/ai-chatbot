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


async def enqueue_text(
    db: AsyncSession,
    instance_pk: int,
    wa_chat_id: str,
    text: str,
    outbound_message_id: uuid.UUID,
    correlation_id: str | None = None,
) -> bool:
    """Queue a text send; returns False if this id is already queued."""
    stmt = (
        pg_insert(WaOutbox)
        .values(
            instance_id=instance_pk,
            outbound_message_id=outbound_message_id,
            wa_chat_id=wa_chat_id,
            kind="text",
            body=text,
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(constraint="uq_wa_outbox_outbound_message_id")
        .returning(WaOutbox.id)
    )
    inserted = (await db.execute(stmt)).scalar_one_or_none()
    return inserted is not None


def _backoff(settings: Settings, retry_count: int) -> timedelta:
    base = settings.outbox_retry_base_seconds * (2**retry_count)
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
        """Attempt due sends; returns how many rows changed state."""
        handled = 0
        async with self._sessionmaker() as db:
            rows = (
                (
                    await db.execute(
                        select(WaOutbox)
                        .where(
                            WaOutbox.state.in_(("PENDING", "FAILED_RETRYABLE")),
                            WaOutbox.next_attempt_at <= datetime.now(UTC),
                        )
                        .order_by(WaOutbox.id)
                        .limit(_FLUSH_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                await self._attempt(db, row)
                handled += 1
            await db.commit()
        return handled

    async def _attempt(self, db: AsyncSession, row: WaOutbox) -> None:
        log = logger.bind(
            outbox_id=row.id,
            outbound_message_id=str(row.outbound_message_id),
            correlation_id=row.correlation_id,
        )
        if self._settings.dry_run:
            # Acceptance mode: fully processed, deliberately not sent.
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
        exhausted = row.retry_count >= self._settings.retry_max_attempts
        if retryable and not exhausted:
            row.state = "FAILED_RETRYABLE"
            row.next_attempt_at = datetime.now(UTC) + _backoff(self._settings, row.retry_count)
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
