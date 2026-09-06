"""Agent instructions (versions, activation=rollback, sandbox honesty)
and the Лимиты API (validated ranges, live limits, fx/price journals)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
from app.main import create_app
from app.models import AiCall, BudgetLedger, Chat, FxRate, Message, WhatsappInstance
from app.models.enums import (
    BudgetScope,
    CallType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from asgi_lifespan import LifespanManager
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.conftest import login

INSTRUCTION = {
    "system_prompt": "Ты — вежливый консультант центра. Отвечай только по услугам.",
    "style": "Коротко, по делу.",
    "fallback_templates": {"handoff": {"ru": "Передал менеджеру."}},
    "max_output_tokens": 300,
    "reasoning_effort": "low",
}


# --------------------------- instructions ---------------------------


async def test_instruction_versions_and_rollback(authed_client) -> None:
    client, headers = authed_client
    v1 = (await client.post("/api/agent/instructions", json=INSTRUCTION, headers=headers)).json()
    assert v1["version"] == 1
    assert v1["is_active"] is False  # never active on creation

    await client.post(f"/api/agent/instructions/{v1['id']}/activate", headers=headers)
    v2_payload = {**INSTRUCTION, "system_prompt": "Вторая версия инструкции для проверки."}
    v2 = (
        await client.post("/api/agent/instructions", json=v2_payload, headers=headers)
    ).json()
    await client.post(f"/api/agent/instructions/{v2['id']}/activate", headers=headers)

    listing = (await client.get("/api/agent/instructions")).json()
    assert listing["active_version"] == 2
    assert [item["version"] for item in listing["items"]] == [2, 1]

    # Rollback IS activating the old version.
    await client.post(f"/api/agent/instructions/{v1['id']}/activate", headers=headers)
    listing = (await client.get("/api/agent/instructions")).json()
    assert listing["active_version"] == 1
    actives = [item for item in listing["items"] if item["is_active"]]
    assert len(actives) == 1  # the partial unique index guarantees it


async def test_sandbox_is_honest_without_key_and_instruction(authed_client) -> None:
    client, headers = authed_client
    # No instruction yet -> 422 with a clear message.
    missing = await client.post(
        "/api/agent/sandbox", json={"question": "Сколько стоит зал?"}, headers=headers
    )
    assert missing.status_code == 422

    created = (
        await client.post("/api/agent/instructions", json=INSTRUCTION, headers=headers)
    ).json()
    await client.post(f"/api/agent/instructions/{created['id']}/activate", headers=headers)
    # Instruction present but no OpenAI key in tests -> 503, not a fake answer.
    response = await client.post(
        "/api/agent/sandbox", json={"question": "Сколько стоит зал?"}, headers=headers
    )
    assert response.status_code == 503
    assert "OpenAI" in response.json()["detail"]


async def test_sandbox_runs_and_accounts_money_with_chat_id_null(
    settings, clean_db, admin_factory, db_engine
) -> None:
    """Design review §9: sandbox spending is real spending — the ai_calls
    row exists with chat_id NULL; no chat, no outbox row is touched."""
    from decimal import Decimal as D

    from app.ai.chat_provider import FakeChatProvider, make_result
    from app.services.agent import activate_version, create_version
    from app.services.pricing import seed_default_prices
    from tests.integration.test_kb_rag import ScriptedEmbeddings, unit_vector

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(FxRate(pair="USD/KZT", rate=D("500"), effective_from=datetime.now(UTC)))
        await seed_default_prices(db, settings)
        instruction = await create_version(
            db,
            system_prompt="Ты — консультант центра, отвечай по делу.",
            style=None,
            fallback_templates={},
            escalation_rules=None,
            max_output_tokens=None,
            reasoning_effort=None,
            author_user_id=None,
        )
        await activate_version(db, instruction, actor_user_id=None)
    # An eligible document so RAG has something to retrieve.
    from app.models import KbDocument
    from app.models.enums import DocStatus, KbLanguage, SourceType
    from app.services.kb_indexing import index_document

    question = "сколько стоит абонемент"
    body_text = "Абонемент стоит 9000 тенге."

    class PricedScripted(ScriptedEmbeddings):
        model = "text-embedding-3-small"

    embeddings = PricedScripted({question: unit_vector(0.0), body_text: unit_vector(0.05)})
    async with maker() as db, db.begin():
        document = KbDocument(
            title="Абонементы",
            body=body_text,
            category="gym",
            language=KbLanguage.RU,
            source_type=SourceType.MANUAL_TEXT,
            status=DocStatus.ACTIVE,
            is_searchable=True,
            index_state="pending",
        )
        db.add(document)
        await db.flush()
        await index_document(db, document, embeddings, settings)

    chat_provider = FakeChatProvider(script=[make_result("Абонемент — 9000 тенге.")])
    app = create_app(
        settings,
        redis=FakeRedis(),
        embedding_provider=embeddings,
        chat_provider=chat_provider,
    )
    admin = await admin_factory()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            csrf = await login(client, settings, admin)
            response = await client.post(
                "/api/agent/sandbox",
                json={"question": question},
                headers={"X-CSRF-Token": csrf},
            )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "model"
    assert payload["text"] == "Абонемент — 9000 тенге."
    assert Decimal(payload["cost_usd"]) > 0
    assert payload["kb_chunk_ids"]

    async with maker() as db:
        calls = (
            (await db.execute(select(AiCall).where(AiCall.call_type == CallType.CHAT)))
            .scalars()
            .all()
        )
        day = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
    assert len(calls) == 1
    assert calls[0].chat_id is None  # sandbox: accounted, chat-less
    assert calls[0].correlation_id.startswith("sandbox:")
    assert day.spent_usd > 0


# ------------------------------ limits ------------------------------


async def test_limits_roundtrip_and_validation(authed_client, db_engine) -> None:
    client, headers = authed_client
    snapshot = (await client.get("/api/limits")).json()
    assert snapshot["settings"]["daily_ai_budget_kzt"] == 300
    assert snapshot["dry_run"] is True
    assert snapshot["usd_kzt_rate"] is None  # honest: the owner has not set it

    # Range validation refuses nonsense instead of storing it.
    bad = dict(snapshot["settings"], max_output_tokens=99_999)
    response = await client.put("/api/limits", json={"settings": bad}, headers=headers)
    assert response.status_code == 422

    updated_settings = dict(snapshot["settings"], daily_ai_budget_kzt=500)
    response = await client.put(
        "/api/limits", json={"settings": updated_settings}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["settings"]["daily_ai_budget_kzt"] == 500


async def test_budget_edit_applies_to_open_period(authed_client, db_engine) -> None:
    """Design review §6: today's ledger limit follows the edited setting."""
    client, headers = authed_client
    await client.post("/api/limits/fx", json={"rate": "500"}, headers=headers)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    today = datetime.now(UTC).date()
    async with maker() as db, db.begin():
        db.add(
            BudgetLedger(
                scope=BudgetScope.GLOBAL_DAY,
                scope_key=today.isoformat(),
                limit_usd=Decimal("0.6"),
                period_start=today,
                period_end=today,
            )
        )
    snapshot = (await client.get("/api/limits")).json()
    updated = dict(snapshot["settings"], daily_ai_budget_kzt=1000)  # = $2 at 500
    await client.put("/api/limits", json={"settings": updated}, headers=headers)
    async with maker() as db:
        ledger = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
    assert ledger.limit_usd == Decimal("2.000000")


async def test_fx_and_price_are_journals(authed_client, db_engine) -> None:
    client, headers = authed_client
    await client.post("/api/limits/fx", json={"rate": "500"}, headers=headers)
    await client.post("/api/limits/fx", json={"rate": "520"}, headers=headers)
    await client.post(
        "/api/limits/prices",
        json={"model": "gpt-5.6-luna", "unit": "per_1m_input", "price_usd": "0.25"},
        headers=headers,
    )
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        rates = (await db.execute(select(FxRate).order_by(FxRate.id))).scalars().all()
    assert [r.rate for r in rates] == [Decimal("500.0000"), Decimal("520.0000")]
    snapshot = (await client.get("/api/limits")).json()
    assert snapshot["usd_kzt_rate"] == "520.0000"  # latest wins, history kept


# --------------------------- почему так -----------------------------


async def test_message_ai_call_details(authed_client, db_engine) -> None:
    client, headers = authed_client
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        instance = WhatsappInstance(
            provider="green_api", instance_id="1101000002", token_ref="GREEN_API_TOKEN"
        )
        db.add(instance)
        await db.flush()
        chat = Chat(instance_id=instance.id, wa_chat_id="77010000001@c.us", phone="+77010000001")
        db.add(chat)
        await db.flush()
        message = Message(
            chat_id=chat.id,
            direction=MsgDirection.OUT,
            source=MsgSource.AI,
            body="Ответ",
            state=MsgState.SENT,
        )
        db.add(message)
        await db.flush()
        db.add(
            AiCall(
                correlation_id="c1",
                chat_id=chat.id,
                message_id=message.id,
                call_type=CallType.CHAT,
                model="gpt-5.6-luna",
                input_tokens=1000,
                output_tokens=100,
                cost_usd=Decimal("0.00032"),
                cost_kzt=Decimal("0.16"),
                latency_ms=900,
                instruction_version=1,
            )
        )
        chat_id, message_id = chat.id, message.id

    found = (await client.get(f"/api/chats/{chat_id}/messages/{message_id}/ai-call")).json()
    assert found["found"] is True
    assert found["call"]["input_tokens"] == 1000
    assert found["call"]["cost_kzt"] == "0.1600"
    assert found["call"]["instruction_version"] == 1

    # A message without a paid call answers honestly, not with a 500.
    async with maker() as db, db.begin():
        other = Message(
            chat_id=chat_id,
            direction=MsgDirection.OUT,
            source=MsgSource.AI,
            body="Из FAQ",
            state=MsgState.SENT,
        )
        db.add(other)
        await db.flush()
        other_id = other.id
    empty = (await client.get(f"/api/chats/{chat_id}/messages/{other_id}/ai-call")).json()
    assert empty == {"found": False, "call": None}
