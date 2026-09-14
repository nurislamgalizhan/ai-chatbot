"""Orchestration behind `python -m app.cli eval|load-test|preflight`.

Kept out of `cli.py` so each run is one readable function: create the
scratch database, wire the fake (or real) providers, run, tear down.
"""

from __future__ import annotations

import secrets
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pyotp
from redis.asyncio import Redis

from app.db.engine import build_engine, build_sessionmaker
from app.eval.fixture import SeededFixture, seed_fixture
from app.eval.load import VOICE_TRANSCRIPT, LoadHarness, LoadReport, make_wav
from app.eval.offline import (
    OFFLINE_RAG_MIN_SCORE,
    FileServingProvider,
    LexicalEmbeddings,
    OracleChatProvider,
    RecordingChatProvider,
    StaticTranscription,
)
from app.eval.runner import EvalReport, EvalSuite, run_suite
from app.eval.scratch import (
    ScratchDatabase,
    create_scratch_database,
    drop_scratch_database,
    migrate,
    scratch_redis_url,
)
from app.services.generation import GenerationDeps

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.config import Settings

_HTTP_OK = 200
_LOAD_ADMIN_PHONE = "+70000000001"


@dataclass
class ScratchRun:
    """Everything an offline run needs, living in the scratch database."""

    scratch: ScratchDatabase
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    embeddings: LexicalEmbeddings
    seeded: SeededFixture


@asynccontextmanager
async def scratch_run(
    settings: Settings,
    suite: EvalSuite,
    *,
    keep_db: bool,
    overrides: dict[str, object] | None = None,
    pool_size: int = 10,
) -> AsyncIterator[ScratchRun]:
    """Create + migrate + seed a scratch database; drop it afterwards."""
    scratch = await create_scratch_database(settings.database_url)
    print(f"Временная база: {scratch.name}", file=sys.stderr)
    engine: AsyncEngine | None = None
    redis: Redis | None = None
    try:
        await migrate(scratch.url)
        run_settings = settings.model_copy(
            update={
                "database_url": scratch.url,
                "redis_url": scratch_redis_url(settings.redis_url),
                "rag_min_score": OFFLINE_RAG_MIN_SCORE,
                "dry_run": True,
                **(overrides or {}),
            }
        )
        engine = build_engine(run_settings, pool_size=pool_size, max_overflow=10)
        sessionmaker = build_sessionmaker(engine)
        redis = Redis.from_url(run_settings.redis_url)
        await redis.flushdb()
        embeddings = LexicalEmbeddings(model=settings.openai_embedding_model)
        seeded = await seed_fixture(sessionmaker, run_settings, suite.fixture, embeddings)
        yield ScratchRun(scratch, run_settings, engine, sessionmaker, redis, embeddings, seeded)
    finally:
        if redis is not None:
            await redis.flushdb()
            await redis.aclose()
        if engine is not None:
            await engine.dispose()
        if keep_db:
            print(f"База оставлена по запросу: {scratch.name}", file=sys.stderr)
        else:
            await drop_scratch_database(scratch)


async def run_offline_eval(settings: Settings, suite: EvalSuite, *, keep_db: bool) -> EvalReport:
    async with scratch_run(settings, suite, keep_db=keep_db) as run:
        oracle = OracleChatProvider()
        deps = GenerationDeps(
            sessionmaker=run.sessionmaker,
            settings=run.settings,
            redis=run.redis,
            chat_provider=oracle,
            embedding_provider=run.embeddings,
        )
        report = await run_suite(deps, suite, mode="offline", requests=oracle.requests)
    report.notes.insert(
        0,
        f"Эталонная база: документов {run.seeded.documents}, выучено {run.seeded.indexed}, "
        f"фрагментов {run.seeded.chunks}, записей FAQ {run.seeded.faq}.",
    )
    return report


def confirm_live_eval(settings: Settings, suite: EvalSuite, *, yes: bool) -> bool:
    paid = sum(1 for c in suite.cases if c.expect.kinds != ["faq"])
    print(
        f"ЖИВОЙ прогон: {len(suite.cases)} кейсов, до {paid} платных вызовов модели "
        f"({settings.openai_chat_model}) против ВАШЕЙ базы знаний и ВАШИХ лимитов. "
        "Расходы попадут в «Расходы» как работа в панели.",
        file=sys.stderr,
    )
    if yes:
        return True
    answer = input("Продолжить? [y/N] ").strip().lower()
    return answer in ("y", "yes", "д", "да")


async def run_live_eval(settings: Settings, suite: EvalSuite) -> EvalReport:
    from app.ai.chat_provider import OpenAIChatProvider  # noqa: PLC0415 - needs a key
    from app.ai.embeddings import OpenAIEmbeddingProvider  # noqa: PLC0415

    recorder = RecordingChatProvider(OpenAIChatProvider(settings))
    embeddings = OpenAIEmbeddingProvider(settings)
    engine = build_engine(settings)
    redis = Redis.from_url(settings.redis_url)
    try:
        deps = GenerationDeps(
            sessionmaker=build_sessionmaker(engine),
            settings=settings,
            redis=redis,
            chat_provider=recorder,
            embedding_provider=embeddings,
        )
        report = await run_suite(deps, suite, mode="live", requests=recorder.requests)
    finally:
        await recorder.aclose()
        await embeddings.aclose()
        await redis.aclose()
        await engine.dispose()
    report.notes.insert(
        0,
        "Ожидания построены по эталонной базе знаний фазы приёмки; расхождения с вашей базой "
        "(неутверждённые переводы, неактивные документы) проявятся как непройденные кейсы "
        "FAQ и RAG.",
    )
    return report


@asynccontextmanager
async def panel_probe(
    run: ScratchRun, oracle: OracleChatProvider
) -> AsyncIterator[Callable[[], Awaitable[bool]]]:
    """A logged-in panel client in the same process.

    Measures whether the event loop stays responsive while every worker
    stage runs; in production the API is a separate container, so this
    is an upper bound, not the real latency.
    """
    from app.main import create_app  # noqa: PLC0415 - heavy import, CLI only
    from app.services.admin_setup import create_admin  # noqa: PLC0415

    password = secrets.token_urlsafe(18)
    async with run.sessionmaker() as db, db.begin():
        admin = await create_admin(db, run.settings, _LOAD_ADMIN_PHONE, "Load Test", password)
    app = create_app(
        run.settings,
        redis=run.redis,
        embedding_provider=run.embeddings,
        chat_provider=oracle,
        configure_logs=False,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://loadtest") as client:
            step1 = await client.post(
                "/api/auth/login", json={"phone": admin.phone, "password": password}
            )
            step1.raise_for_status()
            step2 = await client.post(
                "/api/auth/verify-totp",
                json={
                    "challenge_token": step1.json()["challenge_token"],
                    "code": pyotp.TOTP(admin.totp_secret).now(),
                },
            )
            step2.raise_for_status()

            async def probe() -> bool:
                response = await client.get("/api/chats?limit=20")
                return response.status_code == _HTTP_OK

            yield probe


async def run_load_test(
    settings: Settings,
    suite: EvalSuite,
    *,
    chats: list[int],
    voice_every: int,
    latency: float,
    keep_db: bool,
) -> LoadReport:
    from app.services.media_processing import MediaDeps, MediaProcessor  # noqa: PLC0415
    from app.whatsapp.outbox import OutboxFlusher  # noqa: PLC0415
    from app.worker.pipeline import AiPipeline  # noqa: PLC0415

    report = LoadReport(started_at=datetime.now(UTC), model_latency_seconds=latency)
    with tempfile.TemporaryDirectory(prefix="wabot-load-") as media_dir:
        overrides: dict[str, object] = {
            "media_dir": media_dir,
            # The probe logs into the in-process API; prod settings demand
            # an explicit origin list even though no browser is involved.
            "cors_origins": settings.cors_origins or "https://loadtest",
        }
        async with scratch_run(
            settings, suite, keep_db=keep_db, overrides=overrides, pool_size=12
        ) as run:
            oracle = OracleChatProvider(latency_seconds=latency)
            deps = GenerationDeps(
                sessionmaker=run.sessionmaker,
                settings=run.settings,
                redis=run.redis,
                chat_provider=oracle,
                embedding_provider=run.embeddings,
            )
            media = MediaProcessor(
                MediaDeps(
                    sessionmaker=run.sessionmaker,
                    settings=run.settings,
                    redis=run.redis,
                    provider=FileServingProvider(make_wav(6.0)),
                    chat_provider=oracle,
                    transcription_provider=StaticTranscription(
                        VOICE_TRANSCRIPT, model=settings.openai_transcribe_model
                    ),
                )
            )
            async with panel_probe(run, oracle) as probe:
                harness = LoadHarness(
                    sessionmaker=run.sessionmaker,
                    engine=run.engine,
                    instance_pk=run.seeded.instance_pk,
                    chat_provider=oracle,
                    pipeline=AiPipeline(deps),
                    media_processor=media,
                    outbox=OutboxFlusher(run.sessionmaker, None, run.settings),
                    probe=probe,
                )
                for count in chats:
                    print(f"Нагрузка: {count} чатов…", file=sys.stderr)
                    report.rounds.extend(await harness.run(count, voice_every=voice_every))
            report.notes.append(
                f"Эталонная база: выучено {run.seeded.indexed} документов, записей FAQ "
                f"{run.seeded.faq}; голосовое — каждое {voice_every}-е сообщение "
                "(WAV 6 с, распознавание заглушкой)."
            )
    return report
