"""Application factory. The API process serves the admin panel only."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text

from app.api import (
    account,
    agent,
    analytics,
    auth,
    chats,
    kb,
    limits,
    system,
    system_settings,
    system_status,
    tags,
)
from app.config import Settings, get_settings
from app.db.engine import build_engine, build_sessionmaker
from app.middleware import CsrfAndOriginMiddleware, SecurityHeadersMiddleware
from app.observability.logging import CorrelationIdMiddleware, configure_logging
from app.services.admin_setup import count_active_admins

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = structlog.get_logger(__name__)


class StartupCheckError(RuntimeError):
    pass


async def run_startup_checks(app: FastAPI) -> None:
    """Fail fast on a broken deployment instead of limping along."""
    async with app.state.sessionmaker() as db:
        await db.execute(text("SELECT 1"))
        active_admins = await count_active_admins(db)
    if active_admins > 1:
        msg = (
            f"Found {active_admins} active admin accounts; exactly one is allowed. "
            "Deactivate extras directly in the database before starting."
        )
        raise StartupCheckError(msg)
    await app.state.redis.ping()


def _build_embedding_provider(settings: Settings) -> object | None:
    if not settings.openai_api_key:
        logger.warning("openai_not_configured", detail="kb search preview disabled")
        return None
    from app.ai.embeddings import OpenAIEmbeddingProvider  # noqa: PLC0415

    return OpenAIEmbeddingProvider(settings)


def _build_chat_provider(settings: Settings) -> object | None:
    """The API's own provider instance — used ONLY by the agent sandbox
    (the worker has its own); chat viewing never calls OpenAI."""
    if not settings.openai_api_key:
        return None
    from app.ai.chat_provider import OpenAIChatProvider  # noqa: PLC0415

    return OpenAIChatProvider(settings)


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    embedding_provider: object | None = None,
    chat_provider: object | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.engine = build_engine(settings)
        app.state.sessionmaker = build_sessionmaker(app.state.engine)
        app.state.redis = redis if redis is not None else Redis.from_url(settings.redis_url)
        # The API only PREVIEWS retrieval (it embeds the query); indexing
        # and all chat calls belong to the worker.
        app.state.embedding_provider = embedding_provider or _build_embedding_provider(settings)
        app.state.chat_provider = chat_provider or _build_chat_provider(settings)
        await run_startup_checks(app)
        logger.info("app_started", env=settings.app_env, dry_run=settings.dry_run)
        try:
            yield
        finally:
            if app.state.chat_provider is not None:
                await app.state.chat_provider.aclose()
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    docs_kwargs = (
        {} if settings.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    app = FastAPI(title="Admin API", lifespan=lifespan, **docs_kwargs)  # type: ignore[arg-type]

    # Middleware runs in reverse registration order: correlation id first,
    # then CSRF/Origin, then headers on the way out.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(CsrfAndOriginMiddleware, settings=settings)
    app.add_middleware(CorrelationIdMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,  # exact strings, never "*"
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        )

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(chats.router)
    app.include_router(tags.router)
    app.include_router(system_settings.router)
    app.include_router(kb.router)
    app.include_router(agent.router)
    app.include_router(limits.router)
    app.include_router(analytics.router)
    app.include_router(system_status.router)
    return app
