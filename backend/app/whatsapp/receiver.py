"""Polling receiver: GREEN-API queue -> inbound_notifications inbox.

Hard ordering (SPEC §15): store -> COMMIT -> DeleteNotification. If the
delete fails, the same notification returns on the next poll and hits the
UNIQUE constraint — logged as a duplicate, deleted again, never reprocessed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from app.models import InboundNotification, SystemEvent, WhatsappInstance
from app.services.alerts import hour_bucket, record_alert
from app.whatsapp.classify import IGNORED_KINDS, classify
from app.whatsapp.provider import RawNotification

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import Settings
    from app.whatsapp.provider import WhatsAppProvider

logger = structlog.get_logger(__name__)

#: Errors whose text may contain the customer's own words: a database
#: error carries the failing SQL and its parameters, and a provider error
#: can echo the payload back. `last_error` is shown on the «Система»
#: page, so only the exception TYPE crosses that line (SPEC invariant 9).
_OPAQUE_ERRORS = ("sqlalchemy", "asyncpg", "psycopg")


def _safe_error(error: Exception) -> str:
    module = type(error).__module__.split(".")[0].lower()
    if module in _OPAQUE_ERRORS:
        return f"{type(error).__name__} (подробности в логах сервера)"
    return str(error)[:300]


GREEN_API_TOKEN_REF = "GREEN_API_TOKEN"  # noqa: S105  # nosec B105 - env var NAME

# last_polled_at is persisted at most this often to avoid a write per poll.
_LAST_POLLED_WRITE_INTERVAL_SECONDS = 30


async def ensure_instance_registered(db: AsyncSession, settings: Settings) -> int | None:
    """Upsert the configured instance row; returns its id (or None)."""
    if not settings.green_api_id_instance:
        return None
    stmt = (
        pg_insert(WhatsappInstance)
        .values(
            provider="green_api",
            instance_id=settings.green_api_id_instance,
            token_ref=GREEN_API_TOKEN_REF,
            is_active=True,
        )
        .on_conflict_do_update(
            constraint="uq_whatsapp_instances_provider",
            set_={"token_ref": GREEN_API_TOKEN_REF, "is_active": True},
        )
        .returning(WhatsappInstance.id)
    )
    instance_id = (await db.execute(stmt)).scalar_one()
    await db.flush()
    return int(instance_id)


async def get_active_instance(db: AsyncSession) -> WhatsappInstance | None:
    return (
        await db.execute(
            select(WhatsappInstance)
            .where(WhatsappInstance.is_active)
            .order_by(WhatsappInstance.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def store_notification(
    db: AsyncSession, instance_pk: int, notification: RawNotification
) -> bool:
    """Insert into the inbox; returns False on a duplicate (already stored)."""
    classified = classify(notification.receipt_id, notification.body)
    status = "ignored" if classified.kind in IGNORED_KINDS else "stored"
    processed_at = datetime.now(UTC) if status == "ignored" else None
    stmt = (
        pg_insert(InboundNotification)
        .values(
            instance_id=instance_pk,
            provider_message_id=classified.dedupe_key,
            notification_type=classified.notification_type,
            raw_payload=notification.body,
            status=status,
            processed_at=processed_at,
        )
        .on_conflict_do_nothing(constraint="uq_inbound_notifications_dedupe")
        .returning(InboundNotification.id)
    )
    inserted_id = (await db.execute(stmt)).scalar_one_or_none()
    if inserted_id is None:
        logger.info(
            "inbound_duplicate",
            dedupe_key=classified.dedupe_key,
            kind=classified.kind.value,
        )
        return False
    logger.info(
        "inbound_stored",
        inbound_id=inserted_id,
        kind=classified.kind.value,
        notification_type=classified.notification_type,
        status=status,
    )
    return True


#: SQLSTATE classes that describe the DATA, not the server: 22 = data
#: exception (a NUL inside JSONB is 22P05), 23 = integrity violation.
#: Everything else (08 connection, 40 rollback, 53/57/58 server trouble)
#: is treated as an outage and redelivered.
_POISON_SQLSTATE_CLASSES = ("22", "23")


def _rejected_by_database(exc: SQLAlchemyError) -> bool:
    """Is this a deterministic rejection of the payload rather than an
    outage? asyncpg errors are wrapped twice; the SQLSTATE is on the
    innermost exception."""
    if getattr(exc, "connection_invalidated", False):
        return False
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(state, str) and state[:2] in _POISON_SQLSTATE_CLASSES:
            return True
    return isinstance(exc, DataError | IntegrityError)


def _sanitized(notification: RawNotification) -> RawNotification:
    """The same payload without the characters PostgreSQL's JSONB refuses."""
    cleaned = json.loads(json.dumps(notification.body).replace("\\u0000", ""))
    return RawNotification(receipt_id=notification.receipt_id, body=cleaned)


class PollingReceiver:
    """One long-poll cycle per call; the worker loops it forever."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: WhatsAppProvider,
        instance_pk: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = provider
        self._instance_pk = instance_pk
        self._last_polled_written = datetime.min.replace(tzinfo=UTC)

    async def poll_once(self) -> bool:
        """Returns True if a notification was handled (queue may have more).

        Ack discipline (owner decision, 2026-09-01):
        - ProviderError / infra (DB) errors -> NO DeleteNotification: retry
          after redelivery is justified, nothing was persisted.
        - ANY parse/processing error -> the raw payload is preserved as a
          parse_failed inbox row and the notification IS acked. A poison
          notification must never wedge the queue: an un-acked one is
          redelivered on EVERY following receiveNotification and blocks
          all messages behind it — quieter than a crash loop, and worse.
        """
        notification = await self._provider.poll_notification()
        await self._touch_last_polled()
        if notification is None:
            return False

        try:
            async with self._sessionmaker() as db:
                await store_notification(db, self._instance_pk, notification)
                await db.commit()  # commit FIRST...
        except SQLAlchemyError as exc:
            if not _rejected_by_database(exc):
                # Infrastructure failure: nothing persisted, do NOT ack —
                # redelivery is the correct outcome. The loop backs off.
                raise
            # The DATABASE rejected the payload itself (a NUL byte inside
            # JSONB, a value out of range): redelivering it would fail the
            # same way forever and block every message behind it. Preserve
            # what can be preserved and ack.
            logger.exception("inbound_rejected_by_database", receipt_id=notification.receipt_id)
            await self._record_parse_failure(_sanitized(notification))
        except OSError:
            raise
        except Exception:
            # Parse/classification failure: preserve the payload, mark the
            # row parse_failed, and fall through to the ack below.
            logger.exception("inbound_parse_failed", receipt_id=notification.receipt_id)
            await self._record_parse_failure(notification)
        # ...only then acknowledge: a crash between commit and delete means
        # redelivery, which the UNIQUE constraint absorbs.
        await self._provider.delete_notification(notification.receipt_id)
        return True

    async def _record_parse_failure(self, notification: RawNotification) -> None:
        """Best-effort persistence of a poison notification (own session).

        The raw payload lands in inbound_notifications.raw_payload (its
        normal home, covered by the retention wipe) with status
        parse_failed; system_events gets a PII-free pointer to it.
        """
        try:
            async with self._sessionmaker() as db:
                stmt = (
                    pg_insert(InboundNotification)
                    .values(
                        instance_id=self._instance_pk,
                        provider_message_id=f"receipt:{notification.receipt_id}:parse_failed",
                        notification_type="parse_failed",
                        raw_payload=notification.body,
                        status="parse_failed",
                        processed_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(constraint="uq_inbound_notifications_dedupe")
                    .returning(InboundNotification.id)
                )
                inbound_id = (await db.execute(stmt)).scalar_one_or_none()
                db.add(
                    SystemEvent(
                        level="error",
                        component="receiver",
                        event="inbound_parse_failed",
                        # PII-free: the payload itself lives in the inbox row.
                        detail={
                            "inbound_id": inbound_id,
                            "receipt_id": notification.receipt_id,
                        },
                    )
                )
                await db.commit()
        except Exception:
            # Last resort: the ack still proceeds, but say so loudly.
            logger.exception("parse_failure_record_failed", receipt_id=notification.receipt_id)

    async def record_poll_error(self, error: Exception) -> None:
        try:
            async with self._sessionmaker() as db:
                detail = _safe_error(error)
                db.add(
                    SystemEvent(
                        level="error",
                        component="receiver",
                        event="poll_failed",
                        detail={"error": type(error).__name__, "message": detail},
                    )
                )
                instance = await db.get(WhatsappInstance, self._instance_pk)
                if instance is not None:
                    instance.last_error = detail
                # No incoming messages are being collected at all: the bot
                # looks alive and answers nobody. Once an hour, so a
                # backoff loop cannot turn one outage into a message
                # storm on the owner's phone.
                await record_alert(
                    db,
                    kind="system",
                    event_key="poll_failed",
                    period_key=hour_bucket(),
                    detail={"error": type(error).__name__},
                )
                await db.commit()
        except Exception:
            # If the DB itself is down this would raise; the loop is already
            # backing off — don't let bookkeeping kill the worker.
            logger.exception("record_poll_error_failed")

    async def _touch_last_polled(self) -> None:
        now = datetime.now(UTC)
        if (now - self._last_polled_written).total_seconds() < (
            _LAST_POLLED_WRITE_INTERVAL_SECONDS
        ):
            return
        self._last_polled_written = now
        async with self._sessionmaker() as db:
            instance = await db.get(WhatsappInstance, self._instance_pk)
            if instance is not None:
                instance.last_polled_at = now
                instance.last_error = None
            await db.commit()
