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


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
