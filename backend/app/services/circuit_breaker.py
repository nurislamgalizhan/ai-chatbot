"""Redis-backed circuit breaker for the chat provider.

Shared state (design review §7): the worker pipeline and the API sandbox
live in different processes, so the breaker must live in Redis, not in a
Python variable. Free answers (FAQ / cache / guardrail templates) are
produced BEFORE the breaker is consulted, so an open breaker never blocks
them. Half-open: when the cooldown expires the next call is a probe — a
single failure re-opens immediately, a success closes fully.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

_FAILURES_KEY = "ai:breaker:failures"
_OPEN_UNTIL_KEY = "ai:breaker:open_until"
_FAILURE_WINDOW_SECONDS = 900  # consecutive-ish: counter dies after 15 min quiet


class CircuitBreaker:
    def __init__(self, redis: Redis, *, failures_threshold: int, cooldown_minutes: int) -> None:
        self._redis = redis
        self._threshold = max(1, failures_threshold)
        self._cooldown = timedelta(minutes=cooldown_minutes)

    async def is_open(self) -> bool:
        open_until = await self.open_until()
        return open_until is not None and datetime.now(UTC) < open_until

    async def open_until(self) -> datetime | None:
        raw = await self._redis.get(_OPEN_UNTIL_KEY)
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else raw
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            await self._redis.delete(_OPEN_UNTIL_KEY)
            return None

    async def record_failure(self) -> bool:
        """Returns True when this failure OPENED the breaker (the caller
        writes the system_event exactly once)."""
        pipe = self._redis.pipeline(transaction=True)
        pipe.incr(_FAILURES_KEY)
        pipe.expire(_FAILURES_KEY, _FAILURE_WINDOW_SECONDS)
        count, _ = await pipe.execute()
        if int(count) < self._threshold:
            return False
        opened_until = datetime.now(UTC) + self._cooldown
        await self._redis.set(_OPEN_UNTIL_KEY, opened_until.isoformat())
        # Half-open after cooldown: leave the counter one failure below
        # the threshold so the first failed probe re-opens instantly.
        await self._redis.set(_FAILURES_KEY, self._threshold - 1, ex=_FAILURE_WINDOW_SECONDS)
        logger.error("circuit_breaker_opened", until=opened_until.isoformat())
        return True

    async def record_success(self) -> None:
        await self._redis.delete(_FAILURES_KEY, _OPEN_UNTIL_KEY)
