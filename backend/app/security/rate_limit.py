"""Fixed-window rate limiting on Redis (INCR + EXPIRE, atomic pipeline)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


async def allow(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Return True if this hit is within the limit for the current window."""
    pipe = redis.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, window_seconds, nx=True)
    count, _ = await pipe.execute()
    return int(count) <= limit
