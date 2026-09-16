"""Dialog modes (SPEC §7 + §17 «Режимы»): timer auto-return, operator
switching from panel and phone, DISABLED immutability, emergency switch,
history + audit on every transition."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    AuditLog,
    Chat,
    ChatModeHistory,
    InboundNotification,
    WhatsappInstance,
)
from app.models.enums import ChatMode, MsgSource
from app.services.app_settings import (
    PANEL_REPLY_MODE,
    set_setting,
)
from app.services.chat_modes import (
    ai_allowed,
    on_operator_message,
    resume_expired_pauses,
)
from app.services.inbox_processing import process_inbound_batch


@pytest.fixture
async def instance_pk(db_engine) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        row = WhatsappInstance(
            provider="green_api", instance_id="1101000001", token_ref="GREEN_API_TOKEN"
        )
        db.add(row)
        await db.flush()
        return row.id


async def make_chat(
    db_engine,
    instance_pk: int,
    *,
    mode: ChatMode = ChatMode.AUTO_AI,
    paused_until: datetime | None = None,
    suffix: str = "9481555",
) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        chat = Chat(
            instance_id=instance_pk,
            wa_chat_id=f"7701{suffix}@c.us",
            phone=f"+7701{suffix}",
            display_name="Client",
            mode=mode,
            paused_until=paused_until,
        )
        db.add(chat)
        await db.flush()
        return chat.id


def phone_reply_payload(id_message: str) -> dict[str, Any]:
    return {
        "typeWebhook": "outgoingMessageReceived",
        "idMessage": id_message,
        "timestamp": 1756800000,
        "senderData": {"chatId": "77019481555@c.us"},
        "messageData": {
            "typeMessage": "textMessage",
            "textMessageData": {"textMessage": "Отвечаю сам"},
        },
    }


async def test_expired_pause_returns_to_auto_ai_with_history_and_audit(
    db_engine, instance_pk
) -> None:
    chat_id = await make_chat(
        db_engine,
        instance_pk,
        mode=ChatMode.PAUSED_UNTIL,
        paused_until=datetime.now(UTC) - timedelta(minutes=1),
    )
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        assert await resume_expired_pauses(db) == 1
        await db.commit()

    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        history = (
            await db.execute(select(ChatModeHistory).where(ChatModeHistory.chat_id == chat_id))
        ).scalar_one()
        audit = (
            await db.execute(select(AuditLog).where(AuditLog.action == "chat.mode_changed"))
        ).scalar_one()
    assert chat.mode is ChatMode.AUTO_AI
    assert chat.paused_until is None
    assert history.from_mode is ChatMode.PAUSED_UNTIL
    assert history.to_mode is ChatMode.AUTO_AI
    assert history.actor_user_id is None  # the scheduler, not a human
    assert audit.after["changed_by"] == "scheduler"


async def test_future_pause_is_not_resumed(db_engine, instance_pk) -> None:
    await make_chat(
        db_engine,
        instance_pk,
        mode=ChatMode.PAUSED_UNTIL,
        paused_until=datetime.now(UTC) + timedelta(minutes=30),
    )
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        assert await resume_expired_pauses(db) == 0


async def test_panel_message_pauses_ai(authed_client, db_engine, instance_pk) -> None:
    """Default policy (owner's Savvi config): panel reply -> 60 min pause."""
    client, headers = authed_client
    chat_id = await make_chat(db_engine, instance_pk)

    response = await client.post(
        f"/api/chats/{chat_id}/messages",
        json={"body": "Отвечаю из панели", "client_message_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 200

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
    assert chat.mode is ChatMode.PAUSED_UNTIL
    expected = datetime.now(UTC) + timedelta(minutes=60)
    assert abs((chat.paused_until - expected).total_seconds()) < 30


async def test_panel_reply_mode_human_setting(authed_client, db_engine, instance_pk) -> None:
    client, headers = authed_client
    chat_id = await make_chat(db_engine, instance_pk)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await set_setting(db, PANEL_REPLY_MODE, "HUMAN")

    await client.post(
        f"/api/chats/{chat_id}/messages",
        json={"body": "Беру на себя", "client_message_id": str(uuid.uuid4())},
        headers=headers,
    )
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
    assert chat.mode is ChatMode.HUMAN


async def test_phone_reply_pauses_and_extends_without_history_spam(db_engine, instance_pk) -> None:
    """SPEC §7: operator's phone message activates the pause; every next
    message extends the timer; extensions do not create history rows."""
    chat_id = await make_chat(db_engine, instance_pk)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def process_phone_reply(id_message: str) -> None:
        async with maker() as db, db.begin():
            db.add(
                InboundNotification(
                    instance_id=instance_pk,
                    provider_message_id=id_message,
                    notification_type="outgoingMessageReceived",
                    raw_payload=phone_reply_payload(id_message),
                    status="stored",
                )
            )
        async with maker() as db:
            await process_inbound_batch(db)
            await db.commit()

    await process_phone_reply("OP-1")
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        first_until = chat.paused_until
    assert chat.mode is ChatMode.PAUSED_UNTIL
    assert first_until is not None

    # Shrink the timer, then a second phone reply must extend it again.
    async with maker() as db, db.begin():
        chat = await db.get(Chat, chat_id)
        chat.paused_until = datetime.now(UTC) + timedelta(minutes=5)
    await process_phone_reply("OP-2")

    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        history_count = len(
            (await db.execute(select(ChatModeHistory).where(ChatModeHistory.chat_id == chat_id)))
            .scalars()
            .all()
        )
    assert chat.paused_until > datetime.now(UTC) + timedelta(minutes=30)
    assert history_count == 1  # one transition; the extension added no row


async def test_disabled_is_never_overridden(db_engine, instance_pk) -> None:
    """DISABLED не отвечает и не переключается ни при каких условиях."""
    chat_id = await make_chat(db_engine, instance_pk, mode=ChatMode.DISABLED)
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async with maker() as db, db.begin():
        chat = await db.get(Chat, chat_id)
        await on_operator_message(db, chat, source=MsgSource.OPERATOR_PHONE)
        await on_operator_message(db, chat, source=MsgSource.OPERATOR_PANEL)

    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        assert chat.mode is ChatMode.DISABLED
        assert await resume_expired_pauses(db) == 0
    assert ai_allowed(chat, emergency_off=False) is False
    assert ai_allowed(chat, emergency_off=True) is False


def test_ai_allowed_gate() -> None:
    """The single gate for phase 6: emergency mutes everything; only a
    strict AUTO_AI passes; an expired pause still waits for the scheduler."""
    auto = Chat(mode=ChatMode.AUTO_AI)
    human = Chat(mode=ChatMode.HUMAN)
    paused_expired = Chat(
        mode=ChatMode.PAUSED_UNTIL,
        paused_until=datetime.now(UTC) - timedelta(minutes=5),
    )
    assert ai_allowed(auto, emergency_off=False) is True
    assert ai_allowed(auto, emergency_off=True) is False  # emergency mutes ALL
    assert ai_allowed(human, emergency_off=False) is False
    assert ai_allowed(paused_expired, emergency_off=False) is False  # no lazy return


async def test_emergency_switch_api(authed_client, db_engine) -> None:
    client, headers = authed_client

    state = (await client.get("/api/system/ai-switch")).json()
    assert state == {"enabled": True}

    off = await client.put("/api/system/ai-switch", json={"enabled": False}, headers=headers)
    assert off.json() == {"enabled": False}
    assert (await client.get("/api/system/ai-switch")).json() == {"enabled": False}

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        audit = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "settings.changed")))
            .scalars()
            .all()
        )
    assert any(a.entity_id == "emergency_ai_off" for a in audit)

    on = await client.put("/api/system/ai-switch", json={"enabled": True}, headers=headers)
    assert on.json() == {"enabled": True}


async def test_manual_mode_api_and_history(authed_client, db_engine, instance_pk) -> None:
    client, headers = authed_client
    chat_id = await make_chat(db_engine, instance_pk)

    paused = await client.post(
        f"/api/chats/{chat_id}/mode",
        json={"mode": "PAUSED_UNTIL", "pause_minutes": 15},
        headers=headers,
    )
    assert paused.status_code == 200
    assert paused.json()["mode"] == "PAUSED_UNTIL"

    no_minutes = await client.post(
        f"/api/chats/{chat_id}/mode", json={"mode": "PAUSED_UNTIL"}, headers=headers
    )
    assert no_minutes.status_code == 422

    resumed = await client.post(
        f"/api/chats/{chat_id}/mode", json={"mode": "AUTO_AI"}, headers=headers
    )
    assert resumed.json()["mode"] == "AUTO_AI"

    history = (await client.get(f"/api/chats/{chat_id}/mode-history")).json()["items"]
    assert [(h["from_mode"], h["to_mode"]) for h in history] == [
        ("PAUSED_UNTIL", "AUTO_AI"),
        ("AUTO_AI", "PAUSED_UNTIL"),
    ]
    assert all(h["by_admin"] for h in history)

    missing = await client.post("/api/chats/999999/mode", json={"mode": "AUTO_AI"}, headers=headers)
    assert missing.status_code == 404
