"""AI assistance in the knowledge base: translations and trigger phrases.

Owner asked for both to stop being hand-typing (2026-09-11). What these
tests protect is that automating the TYPING did not quietly automate the
DECISION: a machine translation is still a draft no client can ever
receive, and a suggested phrase still has to be accepted by a person.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from app.ai.chat_provider import ChatError, FakeChatProvider, make_result
from app.main import create_app
from app.models import AiCall, BudgetLedger, FxRate, KbFaq, KbFaqPattern
from app.models.enums import BudgetScope, KbLanguage
from app.services.kb_faq import build_pattern
from app.services.pricing import seed_default_prices
from asgi_lifespan import LifespanManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import login

FAQ_PAYLOAD = {
    "normalized_key": "address",
    "category": "org",
    "answer_ru": "Мы находимся по адресу: ул. Коперника 130.",
    "patterns_ru": ["какой у вас адрес"],
    "patterns_kk": ["мекенжайыңыз қандай"],
}
KK_TRANSLATION = "Біз мына мекенжайда орналасқанбыз: Коперник көшесі 130."


@pytest.fixture
def chat_provider() -> FakeChatProvider:
    return FakeChatProvider()


@pytest.fixture
async def ai_client(settings, clean_db, redis, admin_factory, chat_provider, db_engine):
    """Logged-in client on an app that has a (fake) model wired in, plus
    prices and an exchange rate so the paid path is real."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(
            FxRate(pair="USD/KZT", rate=Decimal(500), effective_from=datetime.now(UTC))
        )
        await seed_default_prices(db, settings)

    application = create_app(settings, redis=redis, chat_provider=chat_provider)
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as c:
            admin = await admin_factory()
            csrf = await login(c, settings, admin)
            yield c, {"X-CSRF-Token": csrf}


async def make_faq(client, headers) -> dict:
    response = await client.post("/api/kb/faq", json=FAQ_PAYLOAD, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------- translations ------------------------------


async def test_translation_lands_as_a_draft_the_client_cannot_get(
    ai_client, chat_provider, db_engine
) -> None:
    """The whole point of automating this: the owner stops typing Kazakh,
    but the machine text still needs their approval before any client
    sees it (owner language policy)."""
    client, headers = ai_client
    chat_provider.script = [
        make_result(KK_TRANSLATION, input_tokens=300, output_tokens=60)
    ]
    faq = await make_faq(client, headers)

    response = await client.post(
        f"/api/kb/faq/{faq['id']}/translate", json={"language": "kk"}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["answer_kk"] == KK_TRANSLATION
    assert body["item"]["answer_kk_status"] == "machine"  # NOT approved
    assert Decimal(body["cost_kzt"]) > 0  # the panel tells the owner the price

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        stored = await db.get(KbFaq, faq["id"])
        calls = (await db.execute(select(AiCall))).scalars().all()
        day = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
    # approved_answer() is the only extraction point and it refuses a draft.
    from app.services.kb_faq import approved_answer

    assert approved_answer(stored, KbLanguage.KK) is None
    assert len(calls) == 1
    assert calls[0].cost_usd > 0
    assert day.spent_usd > 0
    assert day.reserved_usd == 0  # settled, nothing leaked


async def test_owner_approval_is_still_one_entry_at_a_time(
    ai_client, chat_provider
) -> None:
    client, headers = ai_client
    chat_provider.script = [make_result(KK_TRANSLATION)]
    faq = await make_faq(client, headers)
    await client.post(
        f"/api/kb/faq/{faq['id']}/translate", json={"language": "kk"}, headers=headers
    )

    approved = await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={"language": "kk", "answer": KK_TRANSLATION, "status": "approved"},
        headers=headers,
    )
    assert approved.json()["answer_kk_status"] == "approved"
    # There is still no endpoint that approves everything at once.
    bulk = await client.post("/api/kb/faq/approve-all", json={}, headers=headers)
    assert bulk.status_code in (404, 405)


async def test_russian_is_never_machine_written(ai_client) -> None:
    """Russian is the source of truth — the owner writes it."""
    client, headers = ai_client
    faq = await make_faq(client, headers)
    response = await client.post(
        f"/api/kb/faq/{faq['id']}/translate", json={"language": "ru"}, headers=headers
    )
    assert response.status_code == 422


async def test_translation_without_a_source_is_refused(
    ai_client, chat_provider
) -> None:
    client, headers = ai_client
    faq = await make_faq(client, headers)
    await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={"language": "ru", "answer": None, "status": "absent"},
        headers=headers,
    )
    response = await client.post(
        f"/api/kb/faq/{faq['id']}/translate", json={"language": "kk"}, headers=headers
    )
    assert response.status_code == 422
    assert chat_provider.requests == []  # nothing paid for


async def test_model_failure_is_honest_and_costs_nothing(
    ai_client, chat_provider, db_engine
) -> None:
    client, headers = ai_client
    chat_provider.script = [ChatError("chat_failed:http_503", retryable=True)]
    faq = await make_faq(client, headers)

    response = await client.post(
        f"/api/kb/faq/{faq['id']}/translate", json={"language": "kk"}, headers=headers
    )

    assert response.status_code == 503  # retryable -> "try again", not a lie
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        stored = await db.get(KbFaq, faq["id"])
        day = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
    assert stored.answer_kk is None
    assert day.spent_usd == 0
    assert day.reserved_usd == 0  # the reservation was released, not stuck


async def test_editing_russian_demotes_an_approved_translation(ai_client) -> None:
    """A real hole in the language policy: without this, correcting the
    Russian answer leaves clients receiving an approved Kazakh
    translation of text that no longer exists."""
    client, headers = ai_client
    faq = await make_faq(client, headers)
    await client.patch(
        f"/api/kb/faq/{faq['id']}/answer",
        json={"language": "kk", "answer": KK_TRANSLATION, "status": "approved"},
        headers=headers,
    )

    updated = await client.patch(
        f"/api/kb/faq/{faq['id']}",
        json={"answer_ru": "Мы переехали: проспект Достык 5."},
        headers=headers,
    )

    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["answer_ru"] == "Мы переехали: проспект Достык 5."
    assert body["answer_ru_status"] == "approved"  # the owner's own text
    assert body["answer_kk_status"] == "machine"  # stale: needs re-approval


# ------------------------------ patterns --------------------------------


async def test_suggested_phrases_are_added_and_duplicates_skipped(
    ai_client, chat_provider, db_engine
) -> None:
    client, headers = ai_client
    chat_provider.script = [
        make_result(
            "1. где вы находитесь\n"
            "- как до вас доехать\n"
            "«какой у вас адрес»\n"  # already exists on this entry
            "адрес центра\n",
            input_tokens=300,
        )
    ]
    faq = await make_faq(client, headers)

    response = await client.post(
        f"/api/kb/faq/{faq['id']}/suggest-patterns",
        json={"language": "ru"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["added"] == 3  # the duplicate was skipped, not 409'd
    phrases = {p["pattern"] for p in body["item"]["patterns"]}
    assert "где вы находитесь" in phrases  # numbering stripped
    assert "как до вас доехать" in phrases  # bullet stripped
    assert "адрес центра" in phrases
    assert "«какой у вас адрес»" not in phrases  # quotes stripped, dup dropped


async def test_a_phrase_owned_by_another_entry_is_never_stolen(
    ai_client, chat_provider, db_engine
) -> None:
    """One phrasing may point at exactly one answer (unique index). A
    suggestion that collides must be skipped quietly, not break the whole
    request — and must not re-route an existing question."""
    client, headers = ai_client
    faq = await make_faq(client, headers)
    other = await client.post(
        "/api/kb/faq",
        json={
            **FAQ_PAYLOAD,
            "normalized_key": "hours",
            "patterns_ru": ["график работы"],
            "patterns_kk": ["жұмыс кестесі"],
        },
        headers=headers,
    )
    assert other.status_code == 200

    chat_provider.script = [make_result("график работы\nкак вас найти\n")]
    response = await client.post(
        f"/api/kb/faq/{faq['id']}/suggest-patterns",
        json={"language": "ru"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["added"] == 1  # only the new one
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        owner_id = (
            await db.execute(
                select(KbFaqPattern.faq_id).where(
                    KbFaqPattern.pattern == "график работы"
                )
            )
        ).scalar_one()
    assert owner_id == other.json()["id"]  # still answers the other entry


async def test_suggestions_in_kazakh_are_stored_for_that_language(
    ai_client, chat_provider
) -> None:
    client, headers = ai_client
    chat_provider.script = [make_result("қайда орналасқансыздар\nмекенжай қандай\n")]
    faq = await make_faq(client, headers)

    response = await client.post(
        f"/api/kb/faq/{faq['id']}/suggest-patterns",
        json={"language": "kk"},
        headers=headers,
    )

    added = [p for p in response.json()["item"]["patterns"] if p["language"] == "kk"]
    assert len(added) == 3  # the original kk pattern plus two suggestions


# ------------------------- editing and deleting -------------------------


async def test_entry_can_be_deleted_with_its_phrases(ai_client, db_engine) -> None:
    """Until now a typo meant an entry nobody could fix or remove, still
    catching customer questions forever."""
    client, headers = ai_client
    faq = await make_faq(client, headers)

    response = await client.delete(f"/api/kb/faq/{faq['id']}", headers=headers)

    assert response.status_code == 204
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        assert await db.get(KbFaq, faq["id"]) is None
        left = (
            (
                await db.execute(
                    select(KbFaqPattern).where(KbFaqPattern.faq_id == faq["id"])
                )
            )
            .scalars()
            .all()
        )
    assert left == []


async def test_pattern_helper_still_owns_normalization(db_engine) -> None:
    """AI output goes through the same build_pattern as hand-typed text —
    matching must not depend on where the phrase came from."""
    pattern = build_pattern(1, KbLanguage.RU, "  Какой У ВАС Адрес?  ")
    assert pattern.normalized_pattern == "какой у вас адрес"
    assert pattern.token_key == sorted({"адрес"})
