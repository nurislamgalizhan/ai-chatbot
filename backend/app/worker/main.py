"""Worker process: GREEN-API polling, outbox flushing, heartbeat.

Later phases add the AI pipeline and schedulers here. The transport layer
never triggers AI itself — it only fills the inbox.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from redis.asyncio import Redis
from sqlalchemy import select

from app.ai.chat_provider import ChatError, ChatProvider, OpenAIChatProvider
from app.ai.embeddings import EmbeddingError, EmbeddingProvider, OpenAIEmbeddingProvider
from app.ai.transcription import (
    OpenAITranscriptionProvider,
    TranscriptionProvider,
)
from app.config import get_settings
from app.db.engine import (
    WORKER_MAX_OVERFLOW,
    WORKER_POOL_SIZE,
    build_engine,
    build_sessionmaker,
)
from app.media import storage
from app.models import Message
from app.observability.logging import configure_logging
from app.services import app_settings
from app.services.alerts import AlertDispatcher
from app.services.app_settings import get_setting
from app.services.budget import sweep_stale_holds
from app.services.chat_modes import resume_expired_pauses
from app.services.generation import GenerationDeps
from app.services.inbox_processing import process_inbound_batch, reconcile_outbox
from app.services.kb_indexing import process_pending_documents
from app.services.media_processing import MediaDeps, MediaProcessor
from app.services.monitoring import check_and_alert, check_provider
from app.services.retention import run_retention
from app.whatsapp.green_api import GreenApiProvider
from app.whatsapp.outbox import OutboxFlusher
from app.whatsapp.provider import ProviderError
from app.whatsapp.receiver import PollingReceiver, ensure_instance_registered
from app.worker.pipeline import AiPipeline

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
_KB_INDEXING_IDLE_SECONDS = 5.0
_PIPELINE_IDLE_SECONDS = 1.0
_RECOVERY_INTERVAL_SECONDS = 60.0
_STALE_HOLD_MINUTES = 15
_MEDIA_IDLE_SECONDS = 2.0
# Retention runs hourly: files live for days, so a tighter schedule
# would only add disk churn on a shared VPS.
_MEDIA_RETENTION_INTERVAL_SECONDS = 3600.0
_MEDIA_RETENTION_BATCH = 200
# Database retention is a daily job by nature (windows are 30-365 days);
# it runs six-hourly so that a worker restarted every day still gets to
# do it, and each pass is a no-op when there is nothing due.
_RETENTION_INTERVAL_SECONDS = 6 * 3600.0
# Alerts must reach the owner quickly — this is the "the bot went quiet"
# channel — but a failed send backs off on its own schedule.
_ALERT_INTERVAL_SECONDS = 20.0
_MONITORING_INTERVAL_SECONDS = 300.0
# The provider's own health: four requests against the owner's plan
# limits, so half-hourly rather than every pass.
_PROVIDER_CHECK_INTERVAL_SECONDS = 1800.0


async def _heartbeat_loop(redis: Redis, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await redis.set(HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=HEARTBEAT_TTL_SECONDS)
        except Exception:
            # Redis restarted or hit maxmemory. Without this guard the
            # exception escapes into the TaskGroup, which cancels every
            # other loop: a blip in a coordination service would take
            # polling, the outbox and the AI pipeline down with it.
            logger.exception("heartbeat_failed")
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
                await db.commit()
            # Own transaction: a reconcile problem must never roll back
            # customer messages that were just parsed.
            async with sessionmaker() as db:
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


async def _kb_indexing_loop(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: EmbeddingProvider | None,
    stop: asyncio.Event,
) -> None:
    """Embeds documents queued by the admin. Without an OpenAI key it
    idles and says so instead of pretending to index."""
    if provider is None:
        logger.warning("openai_not_configured", detail="kb indexing disabled")
        while not stop.is_set():
            await _wait(stop, _IDLE_WITHOUT_INSTANCE_SECONDS)
        return
    while not stop.is_set():
        handled = 0
        try:
            # Sessions are managed inside: one transaction per document, so
            # one broken document cannot re-trigger paid embeddings forever.
            handled = await process_pending_documents(sessionmaker, provider, settings)
        except Exception:
            logger.exception("kb_indexing_error")
        if handled == 0:
            await _wait(stop, _KB_INDEXING_IDLE_SECONDS)


async def _pipeline_loop(pipeline: AiPipeline, stop: asyncio.Event) -> None:
    """QUEUED customer messages -> AI replies (SPEC §6). Survives any
    exception: a crash-loop here would silence the whole bot."""
    while not stop.is_set():
        handled = 0
        try:
            handled = await pipeline.run_once()
        except Exception:
            logger.exception("pipeline_error")
        if handled == 0:
            await _wait(stop, _PIPELINE_IDLE_SECONDS)


async def _recovery_loop(
    pipeline: AiPipeline,
    sessionmaker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> None:
    """Orphaned PROCESSING claims + stale budget holds (SPEC §9.2.4)."""
    while not stop.is_set():
        try:
            recovered = await pipeline.sweep_processing()
            if recovered:
                logger.warning("pipeline_orphans_recovered", count=recovered)
            released = await sweep_stale_holds(
                sessionmaker,
                older_than=datetime.now(UTC) - timedelta(minutes=_STALE_HOLD_MINUTES),
            )
            if released:
                logger.warning("budget_holds_swept", count=released)
        except Exception:
            logger.exception("recovery_loop_error")
        await _wait(stop, _RECOVERY_INTERVAL_SECONDS)


async def _media_loop(processor: MediaProcessor, stop: asyncio.Event) -> None:
    """Voice/image pre-processing (phase 7). Runs ahead of the AI
    pipeline, which waits for a chat's media before answering it."""
    while not stop.is_set():
        handled = 0
        try:
            handled = await processor.run_once()
        except Exception:
            logger.exception("media_loop_error")
        if handled == 0:
            await _wait(stop, _MEDIA_IDLE_SECONDS)


async def _media_retention_loop(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event
) -> None:
    """Delete media files past the retention window; transcripts and
    image descriptions stay forever (SPEC §13).

    Two passes: the DB-driven one nulls media_ref for rows it clears, and
    a filesystem sweep removes files whose row never committed (a crash
    between writing bytes and the commit would otherwise leak them onto a
    124 GB volume shared with the owner's other projects).
    """
    while not stop.is_set():
        try:
            cleared = 0
            async with sessionmaker() as db:
                days = int(await get_setting(db, app_settings.MEDIA_RETENTION_DAYS))
                cutoff = datetime.now(UTC) - timedelta(days=days)
                rows = (
                    (
                        await db.execute(
                            select(Message)
                            .where(
                                Message.media_ref.is_not(None),
                                Message.created_at < cutoff,
                            )
                            .limit(_MEDIA_RETENTION_BATCH)
                        )
                    )
                    .scalars()
                    .all()
                )
                for message in rows:
                    if message.media_ref:
                        await asyncio.to_thread(
                            storage.delete_ref, settings.media_dir, message.media_ref
                        )
                        message.media_ref = None
                        cleared += 1
                await db.commit()
            # The filesystem sweep removes every file past the window,
            # including those whose row this pass did not reach. Running
            # it only when the database pass is CAUGHT UP keeps the two
            # in step: otherwise the panel shows "file unavailable" for
            # messages whose media_ref is still set.
            orphans = 0
            if len(rows) < _MEDIA_RETENTION_BATCH:
                orphans = await asyncio.to_thread(storage.sweep_orphans, settings.media_dir, days)
            if cleared or orphans:
                logger.info("media_retention_run", rows=cleared, orphan_files=orphans)
        except Exception:
            logger.exception("media_retention_error")
        await _wait(stop, _MEDIA_RETENTION_INTERVAL_SECONDS)


async def _retention_loop(
    sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event
) -> None:
    """Database retention (docs/security-and-cost-controls.md §5).

    Deliberately separate from the media loop: that one frees disk, this
    one drops personal data we promised not to keep and debris that would
    otherwise turn a small database into a slow one.
    """
    while not stop.is_set():
        try:
            result = await run_retention(sessionmaker)
            if result.total:
                logger.info("retention_done", **result.counts)
        except Exception:
            logger.exception("retention_error")
        await _wait(stop, _RETENTION_INTERVAL_SECONDS)


async def _alert_loop(sessionmaker: async_sessionmaker[AsyncSession], stop: asyncio.Event) -> None:
    """Queue pending alerts for the owner and read back what happened.

    The send itself belongs to the outbox loop: alerts ride the same
    transport as customer replies, minus the DRY_RUN mute.
    """
    dispatcher = AlertDispatcher(sessionmaker)
    while not stop.is_set():
        try:
            await dispatcher.dispatch_pending()
        except Exception:
            logger.exception("alert_loop_error")
        await _wait(stop, _ALERT_INTERVAL_SECONDS)


async def _monitoring_loop(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: GreenApiProvider | None,
    stop: asyncio.Event,
) -> None:
    """Disk, queue depth, backup freshness -> events and alerts.

    The provider is asked about itself on a slower cadence: those are
    four HTTP calls against the owner's plan limits, and the failures
    they detect (a misconfigured instance, a stuck outgoing queue) do
    not appear in seconds.
    """
    last_provider_check = 0.0
    while not stop.is_set():
        try:
            await check_and_alert(sessionmaker, media_dir=settings.media_dir)
            now = asyncio.get_running_loop().time()
            if now - last_provider_check >= _PROVIDER_CHECK_INTERVAL_SECONDS:
                last_provider_check = now
                await check_provider(sessionmaker, provider)
        except Exception:
            logger.exception("monitoring_error")
        await _wait(stop, _MONITORING_INTERVAL_SECONDS)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run() -> None:  # noqa: PLR0915 - one wiring block per loop; splitting it hides the process layout
    settings = get_settings()
    configure_logging(settings)
    redis = Redis.from_url(settings.redis_url)
    engine = build_engine(settings, pool_size=WORKER_POOL_SIZE, max_overflow=WORKER_MAX_OVERFLOW)
    sessionmaker = build_sessionmaker(engine)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    embedding_provider: EmbeddingProvider | None = None
    chat_provider: ChatProvider | None = None
    if settings.openai_api_key:
        try:
            embedding_provider = OpenAIEmbeddingProvider(settings)
        except EmbeddingError:
            logger.exception("embedding_provider_init_failed")
        try:
            chat_provider = OpenAIChatProvider(settings)
        except ChatError:
            logger.exception("chat_provider_init_failed")

    provider: GreenApiProvider | None = None
    instance_pk: int | None = None
    if settings.green_api_id_instance:
        provider = GreenApiProvider(settings)
        async with sessionmaker() as db:
            instance_pk = await ensure_instance_registered(db, settings)
            await db.commit()

    transcription_provider: TranscriptionProvider | None = None
    if settings.openai_api_key:
        try:
            transcription_provider = OpenAITranscriptionProvider(settings)
        except ChatError:
            logger.exception("transcription_provider_init_failed")

    pipeline = AiPipeline(
        GenerationDeps(
            sessionmaker=sessionmaker,
            settings=settings,
            redis=redis,
            chat_provider=chat_provider,
            embedding_provider=embedding_provider,
        )
    )
    media_processor = MediaProcessor(
        MediaDeps(
            sessionmaker=sessionmaker,
            settings=settings,
            redis=redis,
            provider=provider,
            chat_provider=chat_provider,
            transcription_provider=transcription_provider,
        )
    )

    logger.info(
        "worker_started",
        env=settings.app_env,
        dry_run=settings.dry_run,
        transport_configured=provider is not None,
        embeddings_configured=embedding_provider is not None,
        chat_configured=chat_provider is not None,
        transcription_configured=transcription_provider is not None,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(_heartbeat_loop(redis, stop))
            group.create_task(_polling_loop(settings, sessionmaker, provider, instance_pk, stop))
            group.create_task(_outbox_loop(settings, sessionmaker, provider, stop))
            group.create_task(_domain_sync_loop(sessionmaker, stop))
            group.create_task(_mode_scheduler_loop(sessionmaker, stop))
            group.create_task(_kb_indexing_loop(settings, sessionmaker, embedding_provider, stop))
            group.create_task(_pipeline_loop(pipeline, stop))
            group.create_task(_recovery_loop(pipeline, sessionmaker, stop))
            group.create_task(_media_loop(media_processor, stop))
            group.create_task(_media_retention_loop(settings, sessionmaker, stop))
            group.create_task(_retention_loop(sessionmaker, stop))
            group.create_task(_alert_loop(sessionmaker, stop))
            group.create_task(_monitoring_loop(settings, sessionmaker, provider, stop))
    finally:
        if provider is not None:
            await provider.aclose()
        if embedding_provider is not None:
            await embedding_provider.aclose()
        if chat_provider is not None:
            await chat_provider.aclose()
        if transcription_provider is not None:
            await transcription_provider.aclose()
        await redis.aclose()
        await engine.dispose()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
