"""Container healthcheck for the worker: exit 0 iff the heartbeat is fresh."""

from __future__ import annotations

import asyncio
import sys

from redis.asyncio import Redis

from app.config import get_settings
from app.worker.main import HEARTBEAT_KEY


async def check() -> int:
    redis = Redis.from_url(get_settings().redis_url)
    try:
        value = await redis.get(HEARTBEAT_KEY)
    finally:
        await redis.aclose()
    # The key has a TTL, so its mere presence means the heartbeat is fresh.
    return 0 if value else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
