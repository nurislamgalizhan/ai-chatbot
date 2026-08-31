"""Polling receiver: GREEN-API queue -> inbound_notifications inbox.

Hard ordering (SPEC §15): store -> COMMIT -> DeleteNotification. If the
delete fails, the same notification returns on the next poll and hits the
UNIQUE constraint — logged as a duplicate, deleted again, never reprocessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import InboundNotification, SystemEvent, WhatsappInstance
from app.whatsapp.classify import IGNORED_KINDS, classify

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import Settings
    from app.whatsapp.provider import RawNotification, WhatsAppProvider

logger = structlog.get_logger(__name__)

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
        """Returns True if a notification was handled (queue may have more)."""
        notification = await self._provider.poll_notification()
        await self._touch_last_polled()
        if notification is None:
            return False

        async with self._sessionmaker() as db:
            await store_notification(db, self._instance_pk, notification)
            await db.commit()  # commit FIRST...
        # ...only then acknowledge: a crash between commit and delete means
        # redelivery, which the UNIQUE constraint absorbs.
        await self._provider.delete_notification(notification.receipt_id)
        return True

    async def record_poll_error(self, error: Exception) -> None:
        async with self._sessionmaker() as db:
            db.add(
                SystemEvent(
                    level="error",
                    component="receiver",
                    event="poll_failed",
                    detail={"error": type(error).__name__, "message": str(error)[:300]},
                )
            )
            instance = await db.get(WhatsappInstance, self._instance_pk)
            if instance is not None:
                instance.last_error = str(error)[:300]
            await db.commit()

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
