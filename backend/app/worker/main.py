"""Worker process skeleton (phase 1).

Real work (GREEN-API polling, pipeline, schedulers) arrives in later phases.
What exists now is honest infrastructure: a heartbeat that the container
healthcheck verifies, structured logging, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 90


async def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    redis = Redis.from_url(settings.redis_url)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("worker_started", env=settings.app_env, dry_run=settings.dry_run)
    try:
        while not stop_event.is_set():
            await redis.set(
                HEARTBEAT_KEY,
                datetime.now(UTC).isoformat(),
                ex=HEARTBEAT_TTL_SECONDS,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                continue
    finally:
        await redis.aclose()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
