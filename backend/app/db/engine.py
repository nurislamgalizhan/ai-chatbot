from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from app.config import Settings


#: The worker runs eleven loops plus a pipeline semaphore of 8, so it
#: can ask for far more sessions at once than an HTTP process. With a
#: pool of ten, the extra callers queued behind `pool_timeout` and the
#: loops that lost the race (retention, alerts, recovery) silently made
#: no progress — the kind of stall that looks like "the bot is slow".
WORKER_POOL_SIZE = 12
WORKER_MAX_OVERFLOW = 10


def build_engine(settings: Settings, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        # Fail loudly instead of hanging half a minute per call when the
        # pool is exhausted; every caller here already handles an error.
        pool_timeout=10,
        # A connection idle for an hour on a restarted database is a
        # surprise error later; recycling avoids it.
        pool_recycle=1800,
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
