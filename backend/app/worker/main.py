"""Worker process: GREEN-API polling, outbox flushing, heartbeat.

Later phases add the AI pipeline and schedulers here. The transport layer
never triggers AI itself — it only fills the inbox.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.db.engine import build_engine, build_sessionmaker
from app.observability.logging import configure_logging
from app.services.chat_modes import resume_expired_pauses
from app.services.inbox_processing import process_inbound_batch, reconcile_outbox
from app.whatsapp.green_api import GreenApiProvider
from app.whatsapp.outbox import OutboxFlusher
from app.whatsapp.provider import ProviderError
from app.whatsapp.receiver import PollingReceiver, ensure_instance_registered

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import Settings

logger = structlog.get_logger(__name__)

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 90

_POLL_ERROR_BACKOFF_START = 5.0
_POLL_ERROR_BACKOFF_CAP = 60.0
_IDLE_WITHOUT_INSTANCE_SECONDS = 60.0
_DOMAIN_SYNC_IDLE_SECONDS = 1.0
_MODE_SCHEDULER_INTERVAL_SECONDS = 5.0


async def _heartbeat_loop(redis: Redis, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await redis.set(HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=HEARTBEAT_TTL_SECONDS)
        await _wait(stop, HEARTBEAT_INTERVAL_SECONDS)


async def _polling_loop(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: GreenApiProvider | None,
    instance_pk: int | None,
    stop: asyncio.Event,
) -> None:
    if provider is None or instance_pk is None:
        # Honest unconfigured state: no keys -> no polling, said out loud.
        logger.warning("green_api_not_configured", detail="polling disabled")
        while not stop.is_set():
            await _wait(stop, _IDLE_WITHOUT_INSTANCE_SECONDS)
        return

    receiver = PollingReceiver(sessionmaker, provider, instance_pk)
    error_backoff = _POLL_ERROR_BACKOFF_START
    while not stop.is_set():
        try:
            handled = await receiver.poll_once()
        except ProviderError as exc:
            # Transport failure: nothing persisted, notification will be
            # redelivered — back off and retry.
            logger.error("poll_error", error=str(exc))
            await receiver.record_poll_error(exc)
            await _wait(stop, error_backoff)
            error_backoff = min(error_backoff * 2, _POLL_ERROR_BACKOFF_CAP)
            continue
        except Exception as exc:
            # Infra (DB down) or a bug: the worker must stay alive — a
            # crash loop kills heartbeat/outbox too. Poison payloads never
            # reach this branch (they are absorbed as parse_failed inside
            # poll_once), so whatever lands here is worth retrying.
            logger.exception("poll_unexpected_error")
            await receiver.record_poll_error(exc)
            await _wait(stop, error_backoff)
            error_backoff = min(error_backoff * 2, _POLL_ERROR_BACKOFF_CAP)
            continue
        error_backoff = _POLL_ERROR_BACKOFF_START
        if not handled:
            # Long-poll returned empty; loop again immediately.
            continue


async def _outbox_loop(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: GreenApiProvider | None,
    stop: asyncio.Event,
) -> None:
    flusher = OutboxFlusher(sessionmaker, provider, settings)
    while not stop.is_set():
        try:
            handled = await flusher.flush_once()
        except Exception:
            logger.exception("outbox_flush_error")
            handled = 0
        if handled == 0:
            await _wait(stop, settings.outbox_flush_interval_seconds)


async def _domain_sync_loop(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event
) -> None:
    """Inbox -> chats/messages, plus outbox -> message state reconciliation."""
    while not stop.is_set():
        handled = 0
        try:
            async with sessionmaker() as db:
                handled += await process_inbound_batch(db)
                handled += await reconcile_outbox(db)
                await db.commit()
        except Exception:
            logger.exception("domain_sync_error")
            handled = 0
        if handled == 0:
            await _wait(stop, _DOMAIN_SYNC_IDLE_SECONDS)


async def _mode_scheduler_loop(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event
) -> None:
    """Active return from PAUSED_UNTIL (never a lazy check, SPEC §7)."""
    while not stop.is_set():
        try:
            async with sessionmaker() as db:
                resumed = await resume_expired_pauses(db)
                await db.commit()
            if resumed:
                logger.info("pauses_resumed", count=resumed)
        except Exception:
            logger.exception("mode_scheduler_error")
        await _wait(stop, _MODE_SCHEDULER_INTERVAL_SECONDS)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    redis = Redis.from_url(settings.redis_url)
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    provider: GreenApiProvider | None = None
    instance_pk: int | None = None
    if settings.green_api_id_instance:
        provider = GreenApiProvider(settings)
        async with sessionmaker() as db:
            instance_pk = await ensure_instance_registered(db, settings)
            await db.commit()

    logger.info(
        "worker_started",
        env=settings.app_env,
        dry_run=settings.dry_run,
        transport_configured=provider is not None,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_heartbeat_loop(redis, stop))
            group.create_task(_polling_loop(settings, sessionmaker, provider, instance_pk, stop))
            group.create_task(_outbox_loop(settings, sessionmaker, provider, stop))
            group.create_task(_domain_sync_loop(sessionmaker, stop))
            group.create_task(_mode_scheduler_loop(sessionmaker, stop))
    finally:
        if provider is not None:
            await provider.aclose()
        await redis.aclose()
        await engine.dispose()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
