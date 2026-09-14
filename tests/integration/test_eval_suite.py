"""The acceptance eval set runs in CI, offline, against real PostgreSQL:
every routing criterion in cases.json must hold on the reference
knowledge base. When a case fails here, either the pipeline regressed or
the criterion is wrong — both are worth a red build."""

from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.eval.fixture import seed_fixture
from app.eval.offline import OFFLINE_RAG_MIN_SCORE, LexicalEmbeddings, OracleChatProvider
from app.eval.runner import EvalSuite, load_suite, run_suite
from app.models import AiCall
from app.services.generation import GenerationDeps

RU_FAQ_ADDRESS = "Мы находимся по адресу"


@pytest.fixture
async def eval_env(db_engine, settings):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    suite = load_suite()
    run_settings = settings.model_copy(update={"rag_min_score": OFFLINE_RAG_MIN_SCORE})
    embeddings = LexicalEmbeddings(model=settings.openai_embedding_model)
    seeded = await seed_fixture(maker, run_settings, suite.fixture, embeddings)
    oracle = OracleChatProvider()
    deps = GenerationDeps(
        sessionmaker=maker,
        settings=run_settings,
        redis=FakeRedis(),
        chat_provider=oracle,
        embedding_provider=embeddings,
    )
    return {"suite": suite, "deps": deps, "oracle": oracle, "seeded": seeded, "maker": maker}


async def test_reference_knowledge_base_is_indexed(eval_env) -> None:
    seeded = eval_env["seeded"]
    assert seeded.documents == 9
    assert seeded.indexed == 8  # «Акции» is deliberately not searchable
    assert seeded.chunks >= 8
    assert seeded.faq == 7


async def test_offline_eval_passes_every_case(eval_env) -> None:
    report = await run_suite(
        eval_env["deps"], eval_env["suite"], mode="offline", requests=eval_env["oracle"].requests
    )
    failures = {r.case_id: r.failures for r in report.results if not r.passed}
    assert failures == {}
    assert report.pass_rate == 100.0
    assert report.total == len(eval_env["suite"].cases)
    # Only the paid paths create ai_calls rows; FAQ hits and refusals
    # without a model call must not.
    assert report.spend.calls > 0
    markdown = report.render_markdown()
    assert "# Отчёт eval-прогона" in markdown
    assert "Все кейсы прошли." in markdown
    json.dumps(report.to_dict(), default=str)  # serialisable


async def test_kazakh_question_without_approved_translation(eval_env) -> None:
    """Owner decision: no approved kk answer -> the model answers in
    Kazakh; the Russian FAQ text is never substituted; the miss is
    recorded for the translation analytics."""
    suite: EvalSuite = eval_env["suite"]
    case = next(c for c in suite.cases if c.id == "faq-kk-address-no-approved-kk")
    only = suite.model_copy(update={"cases": [case]})
    report = await run_suite(
        eval_env["deps"], only, mode="offline", requests=eval_env["oracle"].requests
    )
    result = report.results[0]
    assert result.passed, result.failures
    assert result.observed.answer_language == "kk"
    assert result.observed.faq_miss is True
    assert RU_FAQ_ADDRESS not in (result.observed.text or "")
    # The language reached the model explicitly, not by luck.
    request = eval_env["oracle"].requests[-1]
    joined = "\n".join(m["content"] for m in request.messages)
    assert "Язык клиента: kk" in joined or "Язык ответа: kk" in joined


async def test_eval_spend_is_booked_as_owner_work(eval_env) -> None:
    await run_suite(
        eval_env["deps"], eval_env["suite"], mode="offline", requests=eval_env["oracle"].requests
    )
    async with eval_env["maker"]() as db:
        rows = (
            (await db.execute(select(AiCall).where(AiCall.correlation_id.like("eval:%"))))
            .scalars()
            .all()
        )
    assert rows
    assert all(row.chat_id is None for row in rows)
