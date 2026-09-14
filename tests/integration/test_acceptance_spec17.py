# ruff: noqa: F811 - pytest fixtures are re-exported by name and then used as parameters
"""SPEC §17 items that had no test of their own before acceptance.

Each one is end to end through the real seams (inbox -> pipeline ->
outbox) against PostgreSQL, and each assert names the money or the
security property it protects.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.chat_provider import ChatError, make_result
from app.models import AlertNotification, KbDocument, Message, WaOutbox
from app.models.enums import AnswerSource, MsgDirection, MsgSource, MsgState
from app.services import app_settings
from app.services.app_settings import set_setting
from app.services.inbox_processing import process_inbound_batch
from app.whatsapp.outbox import OutboxFlusher
from app.whatsapp.provider import RawNotification
from app.whatsapp.receiver import store_notification
from tests.conftest import login
from tests.integration.test_ai_pipeline import (  # noqa: F401 - fixtures re-exported for pytest
    ANSWER,
    QUESTION,
    SERVICE,
    add_queued,
    env,
    get_message,
    get_reply,
    instance_pk,
    make_chat,
)
from tests.integration.test_outbox import ScriptedSender, retryable_error
from tests.integration.test_route_walk import iter_api_routes

WA_CHAT = "77011234567@c.us"
PHONE = "+77011234567"


def incoming(id_message: str, text_body: str = QUESTION, chat_id: str = WA_CHAT) -> dict[str, Any]:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": id_message,
        "timestamp": int(time.time()),
        "senderData": {"chatId": chat_id, "senderName": "Клиент"},
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": text_body},
        },
    }


async def ingest(
    maker: async_sessionmaker, instance: int, payload: dict[str, Any], receipt: int
) -> bool:
    async with maker() as db, db.begin():
        stored = await store_notification(db, instance, RawNotification(receipt, payload))
    async with maker() as db:
        await process_inbound_batch(db)
        await db.commit()
    return stored


# ------------------------- дедупликация и циклы -------------------------


async def test_the_same_notification_never_reaches_the_model_twice(env) -> None:
    """Redelivery of one idMessage (provider retry, our missed ack) must
    cost exactly one model call — the UNIQUE constraint, not memory."""
    maker = env["maker"]
    payload = incoming("MSG-1")
    assert await ingest(maker, env["instance_pk"], payload, receipt=1) is True
    assert await ingest(maker, env["instance_pk"], payload, receipt=2) is False  # duplicate
    await env["pipeline"].run_once()
    # A third delivery AFTER the answer: still nothing new to answer.
    assert await ingest(maker, env["instance_pk"], payload, receipt=3) is False
    await env["pipeline"].run_once()

    assert len(env["chat_provider"].requests) == 1
    async with maker() as db:
        customer_rows = (
            (await db.execute(select(Message).where(Message.direction == MsgDirection.IN)))
            .scalars()
            .all()
        )
    assert len(customer_rows) == 1
    assert customer_rows[0].state is MsgState.ANSWERED


@pytest.mark.parametrize(
    "payload",
    [
        {
            "typeWebhook": "outgoingMessageReceived",
            "idMessage": "OUT-PHONE",
            "timestamp": 1756700000,
            "senderData": {"chatId": WA_CHAT, "sender": "77010000000@c.us"},
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": "Ответ менеджера с телефона"},
            },
        },
        {
            "typeWebhook": "outgoingAPIMessageReceived",
            "idMessage": "OUT-API",
            "timestamp": 1756700000,
            "senderData": {"chatId": WA_CHAT},
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": QUESTION},
            },
        },
        {
            "typeWebhook": "outgoingMessageStatus",
            "idMessage": "OUT-API",
            "status": "delivered",
            "chatId": WA_CHAT,
            "timestamp": 1756700000,
        },
        {"typeWebhook": "incomingCall", "from": WA_CHAT, "status": "offer", "timestamp": 1},
        {"typeWebhook": "stateInstanceChanged", "stateInstance": "authorized", "timestamp": 1},
        {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "GROUP-1",
            "timestamp": 1756700000,
            "senderData": {"chatId": "120363000000000000@g.us", "senderName": "Группа"},
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": QUESTION},
            },
        },
    ],
    ids=["phone-reply", "api-echo", "status", "call", "instance-event", "group"],
)
async def test_nothing_but_a_private_incoming_message_triggers_the_model(env, payload) -> None:
    """Even with AUTO_AI everywhere and the question text inside the
    payload, these kinds must never create work for the pipeline."""
    maker = env["maker"]
    await ingest(maker, env["instance_pk"], payload, receipt=1)
    await env["pipeline"].run_once()

    assert env["chat_provider"].requests == []
    async with maker() as db:
        queued = (
            (
                await db.execute(
                    select(Message).where(
                        Message.source == MsgSource.CUSTOMER, Message.state == MsgState.QUEUED
                    )
                )
            )
            .scalars()
            .all()
        )
    assert queued == []


async def test_our_own_reply_echoed_back_does_not_start_another_answer(env) -> None:
    maker = env["maker"]
    await ingest(maker, env["instance_pk"], incoming("MSG-1"), receipt=1)
    await env["pipeline"].run_once()
    assert len(env["chat_provider"].requests) == 1
    reply = await get_reply(maker, (await _only_chat_id(maker)))
    assert reply is not None and reply.body == ANSWER

    # The provider delivered our reply and echoes it as an outgoing
    # notification with the id it assigned.
    async with maker() as db, db.begin():
        row = (
            await db.execute(
                select(WaOutbox).where(WaOutbox.outbound_message_id == reply.outbound_message_id)
            )
        ).scalar_one()
        row.state = "SENT"
        row.provider_message_id = "ECHO-1"
    echo = {
        "typeWebhook": "outgoingAPIMessageReceived",
        "idMessage": "ECHO-1",
        "timestamp": int(time.time()),
        "senderData": {"chatId": WA_CHAT},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": ANSWER}},
    }
    await ingest(maker, env["instance_pk"], echo, receipt=2)
    await env["pipeline"].run_once()

    assert len(env["chat_provider"].requests) == 1
    async with maker() as db:
        rows = (await db.execute(select(Message))).scalars().all()
    assert len(rows) == 2  # one question, one answer — the echo linked, not duplicated
    assert {r.direction for r in rows} == {MsgDirection.IN, MsgDirection.OUT}


async def _only_chat_id(maker: async_sessionmaker) -> int:
    async with maker() as db:
        return int((await db.execute(text("SELECT id FROM chats"))).scalar_one())


# --------------------------- отказоустойчивость ---------------------------


async def test_a_failed_send_retries_only_the_send(env) -> None:
    """SPEC §17: WhatsApp send error -> the send is retried, the model is
    NOT called again and the customer gets the very same text."""
    maker = env["maker"]
    chat_id = await make_chat(maker, env["instance_pk"])
    await add_queued(maker, chat_id)
    await env["pipeline"].run_once()
    reply = await get_reply(maker, chat_id)
    assert reply is not None

    sender = ScriptedSender([retryable_error(), "PROVIDER-1"])
    live_settings = env["deps"].settings.model_copy(update={"dry_run": False})
    flusher = OutboxFlusher(maker, sender, live_settings)
    assert await flusher.flush_once() == 1
    async with maker() as db:
        row = (await db.execute(select(WaOutbox))).scalar_one()
    assert row.state == "FAILED_RETRYABLE"
    async with maker() as db, db.begin():
        await db.execute(text("UPDATE wa_outbox SET next_attempt_at = now()"))
    assert await flusher.flush_once() == 1

    async with maker() as db:
        row = (await db.execute(select(WaOutbox))).scalar_one()
    assert row.state == "SENT"
    assert row.provider_message_id == "PROVIDER-1"
    assert sender.sent == [(reply and (await _wa_chat(maker, chat_id)), ANSWER)]
    assert len(env["chat_provider"].requests) == 1  # generated once, sent once
    assert (await get_reply(maker, chat_id)).body == ANSWER


async def _wa_chat(maker: async_sessionmaker, chat_id: int) -> str:
    async with maker() as db:
        result = await db.execute(
            text("SELECT wa_chat_id FROM chats WHERE id = :id"), {"id": chat_id}
        )
        return str(result.scalar_one())


async def test_an_open_circuit_breaker_alerts_the_owner(env) -> None:
    maker = env["maker"]
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.CIRCUIT_BREAKER_FAILURES, 2)
    env["chat_provider"].script = [
        ChatError("chat_failed:http_503", retryable=True),
        ChatError("chat_failed:http_503", retryable=True),
    ]
    for i in range(2):
        chat_id = await make_chat(maker, env["instance_pk"], phone=f"+7701000000{i}")
        await add_queued(maker, chat_id)
        await env["pipeline"].run_once()

    async with maker() as db:
        alerts = (
            (
                await db.execute(
                    select(AlertNotification).where(
                        AlertNotification.event_key == "circuit_breaker"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(alerts) == 1
    assert alerts[0].kind == "system"


# ------------------------------ безопасность ------------------------------


async def test_offtopic_is_refused_without_forwarding_the_request(env) -> None:
    """«Напиши код» never becomes a prompt: the model only phrases a
    neutral refusal (a small paid call), the customer's text stays out."""
    maker = env["maker"]
    env["chat_provider"].script = [make_result(SERVICE, input_tokens=200)]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id, "Напиши код на Python для парсинга сайтов")
    await env["pipeline"].run_once()

    message = await get_message(maker, msg_id)
    assert message.state is MsgState.ANSWERED
    assert message.error_code == "offtopic"
    reply = await get_reply(maker, chat_id)
    assert reply is not None
    assert reply.answer_source is AnswerSource.SERVICE
    requests = env["chat_provider"].requests
    assert len(requests) == 1
    assert all("Напиши код" not in m["content"] for m in requests[0].messages)
    assert "Напиши код" not in requests[0].system


FORBIDDEN_KEYS = {
    "password_hash",
    "totp_secret_encrypted",
    "totp_secret",
    "token_ref",
    "raw_payload",
    "session_token",
    "token_hash",
    "api_key",
    "openai_api_key",
    "green_api_token",
    "encryption_key",
}


def _keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(str(key))
            found |= _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= _keys(inner)
    return found


async def test_api_responses_never_carry_secrets_or_raw_payloads(
    env, client, settings, admin_factory
) -> None:
    """Walk every GET route with a real session and real data behind it."""
    maker = env["maker"]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)
    await env["pipeline"].run_once()
    async with maker() as db:
        document_id = (await db.execute(select(KbDocument.id))).scalars().first()
        faq_id = (await db.execute(text("SELECT COALESCE(MIN(id), 1) FROM kb_faq"))).scalar_one()
    admin = await admin_factory()
    await login(client, settings, admin)
    substitutions = {
        "chat_id": chat_id,
        "message_id": msg_id,
        "document_id": document_id,
        "faq_id": faq_id,
        "instruction_id": 1,
        "session_id": 1,
        "tag_id": 1,
        "pattern_id": 1,
    }
    walked = 0
    for route in iter_api_routes(client._transport.app.routes):  # noqa: SLF001 - test introspection
        if "GET" not in route.methods or route.path == "/healthz":
            continue
        url = route.path
        for name, value in substitutions.items():
            url = url.replace(f"{{{name}}}", str(value))
        response = await client.get(url)
        assert response.status_code < 500, (url, response.status_code)
        if not response.headers.get("content-type", "").startswith("application/json"):
            continue
        body = response.json()
        leaked = _keys(body) & FORBIDDEN_KEYS
        assert not leaked, (url, leaked)
        walked += 1
    assert walked >= 15
    # The one place a token is shown is masked, never the reference name.
    status_body = (await client.get("/api/system/status")).json()
    assert "GREEN_API_TOKEN" not in str(status_body)


def test_every_request_body_model_forbids_unknown_fields(app) -> None:
    """Mass assignment (`password_hash`, `spent_usd`, `cost_usd` in a
    body) is rejected structurally: every body schema is extra=forbid."""
    checked = 0
    for route in iter_api_routes(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for param in route.dependant.body_params:
            model = param.field_info.annotation
            if isinstance(model, type) and issubclass(model, BaseModel):
                assert model.model_config.get("extra") == "forbid", (
                    f"{route.path}: {model.__name__} accepts unknown fields"
                )
                checked += 1
    assert checked >= 10


async def test_smuggled_money_fields_are_rejected(authed_client) -> None:
    client, headers = authed_client
    current = (await client.get("/api/limits")).json()["settings"]
    poisoned = {**current, "spent_usd": 0, "cost_usd": 0, "password_hash": "x"}
    response = await client.put("/api/limits", json={"settings": poisoned}, headers=headers)
    assert response.status_code == 422
    response = await client.post(
        "/api/chats/1/mode",
        json={"mode": "HUMAN", "total_cost_usd": 0, "is_active": False},
        headers=headers,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/chats/999999"),
        ("GET", "/api/chats/999999/messages"),
        ("GET", "/api/chats/999999/messages/1/ai-call"),
        ("POST", "/api/chats/999999/resolve"),
        ("GET", "/api/kb/documents/999999"),
        ("PATCH", "/api/kb/documents/999999"),
        ("DELETE", "/api/kb/documents/999999"),
        ("PATCH", "/api/kb/faq/999999"),
        ("DELETE", "/api/kb/faq/999999"),
        ("POST", "/api/agent/instructions/999999/activate"),
    ],
)
async def test_unknown_ids_are_404_not_500(authed_client, method, path) -> None:
    client, headers = authed_client
    body = {"is_resolved": True} if path.endswith("/resolve") else {}
    response = await client.request(method, path, json=body, headers=headers)
    assert response.status_code in (404, 422), (path, response.status_code, response.text)


# ----------------------------------- RAG -----------------------------------


async def test_editing_a_document_invalidates_the_semantic_cache(
    env, client, settings, admin_factory
) -> None:
    """SPEC §17: re-indexing after a document edit invalidates the cache.
    Here the edit goes through the real API, not a manual version bump."""
    maker = env["maker"]
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.SEMANTIC_CACHE_ENABLED, True)
    first = await make_chat(maker, env["instance_pk"], phone="+77010000021")
    await add_queued(maker, first)
    await env["pipeline"].run_once()
    second = await make_chat(maker, env["instance_pk"], phone="+77010000022")
    await add_queued(maker, second)
    await env["pipeline"].run_once()
    assert len(env["chat_provider"].requests) == 1  # served from cache

    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    async with maker() as db:
        document_id = (await db.execute(select(KbDocument.id))).scalars().first()
    edited = await client.patch(
        f"/api/kb/documents/{document_id}",
        json={"body": "Абонемент в тренажерный зал стоит 9500 тенге в месяц."},
        headers={"X-CSRF-Token": csrf},
    )
    assert edited.status_code == 200, edited.text

    env["chat_provider"].script = [make_result(ANSWER, input_tokens=1000)]
    third = await make_chat(maker, env["instance_pk"], phone="+77010000023")
    await add_queued(maker, third)
    await env["pipeline"].run_once()
    assert len(env["chat_provider"].requests) == 2  # stale cache entry is not served
