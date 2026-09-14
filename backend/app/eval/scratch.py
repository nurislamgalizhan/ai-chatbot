"""A throwaway PostgreSQL database next to the real one.

The offline eval and the load test write chats, messages and money rows
with fake providers. None of that may land in the owner's database (the
reports would show fake spend), so they get their own database on the
same server: created, migrated with the same Alembic history as
production, used, dropped. Redis gets a separate logical database that
is flushed before and after.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{3,60}$")


@dataclass(frozen=True)
class ScratchDatabase:
    name: str
    url: str  # full URL of the scratch database (password included)
    base_url: str  # the configured database, used for CREATE/DROP


async def create_scratch_database(
    base_url: str, *, prefix: str = "wabot_scratch"
) -> ScratchDatabase:
    name = f"{prefix}_{secrets.token_hex(3)}"
    if not _SAFE_NAME.match(name):  # pragma: no cover - defensive, the name is ours
        msg = f"unsafe scratch database name: {name}"
        raise ValueError(msg)
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
            )
    finally:
        await engine.dispose()
    url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    return ScratchDatabase(name=name, url=url, base_url=base_url)


async def drop_scratch_database(scratch: ScratchDatabase) -> None:
    engine = create_async_engine(scratch.base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            # FORCE: our own engines are disposed by now, but a straggling
            # connection must not leave the scratch database behind.
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch.name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def _upgrade_sync(url: str) -> None:
    from alembic.config import Config  # noqa: PLC0415

    from alembic import command  # noqa: PLC0415 - alembic is a CLI-time dependency

    previous = os.environ.get("DATABASE_URL")
    # alembic/env.py reads the URL from Settings, which reads the
    # environment; point it at the scratch database for the duration.
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


async def migrate(url: str) -> None:
    """Run every Alembic migration against the scratch database.

    env.py opens its own event loop (`asyncio.run`), so the upgrade runs
    in a worker thread rather than inside the running loop.
    """
    await asyncio.to_thread(_upgrade_sync, url)


def scratch_redis_url(base_url: str, index: int = 9) -> str:
    """The same Redis server, a different logical database (default /9)."""
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))
