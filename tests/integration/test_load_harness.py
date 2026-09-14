"""A short run of the load harness: the whole chain, five customers,
two waves, a voice note among them. The real numbers come from
`make load-test`; this keeps the harness itself from rotting."""

from __future__ import annotations

from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.eval.fixture import seed_fixture
from app.eval.load import VOICE_TRANSCRIPT, LoadHarness, make_wav
from app.eval.offline import (
    OFFLINE_RAG_MIN_SCORE,
    FileServingProvider,
    LexicalEmbeddings,
    OracleChatProvider,
    StaticTranscription,
)
from app.eval.runner import load_suite
from app.services.generation import GenerationDeps
from app.services.media_processing import MediaDeps, MediaProcessor
from app.whatsapp.outbox import OutboxFlusher
from app.worker.pipeline import AiPipeline


async def test_five_chats_flow_through_the_whole_chain(db_engine, settings, tmp_path) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    suite = load_suite()
    run_settings = settings.model_copy(
        update={
            "rag_min_score": OFFLINE_RAG_MIN_SCORE,
            "dry_run": True,
            "media_dir": str(tmp_path),
        }
    )
    embeddings = LexicalEmbeddings(model=settings.openai_embedding_model)
    seeded = await seed_fixture(maker, run_settings, suite.fixture, embeddings)
    redis = FakeRedis()
    oracle = OracleChatProvider(latency_seconds=0.05)
    deps = GenerationDeps(
        sessionmaker=maker,
        settings=run_settings,
        redis=redis,
        chat_provider=oracle,
        embedding_provider=embeddings,
    )
    media = MediaProcessor(
        MediaDeps(
            sessionmaker=maker,
            settings=run_settings,
            redis=redis,
            provider=FileServingProvider(make_wav(3.0)),
            chat_provider=oracle,
            transcription_provider=StaticTranscription(
                VOICE_TRANSCRIPT, model=settings.openai_transcribe_model
            ),
        )
    )
    probes = {"count": 0}

    async def probe() -> bool:
        probes["count"] += 1
        return True

    harness = LoadHarness(
        sessionmaker=maker,
        engine=db_engine,
        instance_pk=seeded.instance_pk,
        chat_provider=oracle,
        pipeline=AiPipeline(deps),
        media_processor=media,
        outbox=OutboxFlusher(maker, None, run_settings),
        probe=probe,
    )

    rounds = await harness.run(5, voice_every=5, rounds=2, timeout_seconds=60)

    assert len(rounds) == 2
    for round_result in rounds:
        assert round_result.ok, round_result
        assert round_result.chats == 5
        assert round_result.replies == 5
        assert round_result.unanswered == 0
        assert round_result.duplicates == 0
        assert round_result.max_model_concurrency <= 8
        # DRY_RUN: everything was "sent" to nobody.
        assert set(round_result.outbox) == {"SKIPPED_DRY_RUN"}
    assert rounds[0].voice_notes == 1
    assert rounds[0].transcribed == 1
    assert rounds[1].order_violations == 0
    assert probes["count"] > 0
    assert rounds[0].messages_per_second > 0
