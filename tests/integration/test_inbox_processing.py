"""Inbox -> domain: chats/messages creation, sticker ignore, operator
phone replies clearing attention, API echo linking, delivery statuses."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.models import (
    AuditLog,
    Chat,
    InboundNotification,
    Message,
    WaOutbox,
    WhatsappInstance,
)
from app.models.enums import AttentionReason, MsgSource, MsgState
from app.services.chats import set_attention
from app.services.inbox_processing import process_inbound_batch, reconcile_outbox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

WA_CHAT = "77019481555@c.us"


def incoming_payload(
    id_message: str,
    text: str = "Здравствуйте!",
    type_message: str = "textMessage",
    chat_id: str = WA_CHAT,
    webhook: str = "incomingMessageReceived",
) -> dict[str, Any]:
    return {
        "typeWebhook": webhook,
        "idMessage": id_message,
        "timestamp": 1756700000,
        "senderData": {"chatId": chat_id, "senderName": "Айгерим"},
        "messageData": {
            "typeMessage": type_message,
            "textMessageData": {"textMessage": text},
        },
    }


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


async def store(
    db_engine, instance_pk: int, payload: dict[str, Any], dedupe: str
) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(
            InboundNotification(
                instance_id=instance_pk,
                provider_message_id=dedupe,
                notification_type=str(payload.get("typeWebhook")),
                raw_payload=payload,
                status="stored",
            )
        )


async def run_processor(db_engine) -> int:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        n = await process_inbound_batch(db)
        await db.commit()
    return n


async def test_incoming_creates_chat_and_queued_message(db_engine, instance_pk) -> None:
    await store(db_engine, instance_pk, incoming_payload("M1"), "M1")
    assert await run_processor(db_engine) == 1

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        chat = (await db.execute(select(Chat))).scalar_one()
        message = (await db.execute(select(Message))).scalar_one()
        notification = (await db.execute(select(InboundNotification))).scalar_one()

    assert chat.phone == "+77019481555"
    assert chat.display_name == "Айгерим"
    assert chat.unread_count == 1
    assert chat.last_actor.value == "customer"
    assert message.source is MsgSource.CUSTOMER
    assert message.state is MsgState.QUEUED
    assert message.body == "Здравствуйте!"
    assert notification.status == "processed"


async def test_reprocessing_same_message_is_idempotent(db_engine, instance_pk) -> None:
    """Two notifications with one idMessage (upstream dedupe bypassed):
    the partial unique index on messages absorbs the second."""
    await store(db_engine, instance_pk, incoming_payload("M1"), "M1")
    await store(db_engine, instance_pk, incoming_payload("M1"), "M1-redelivered")
    await run_processor(db_engine)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        messages = (await db.execute(select(Message))).scalars().all()
    assert len(messages) == 1


async def test_sticker_message_is_ignored_but_visible(db_engine, instance_pk) -> None:
    await store(
        db_engine,
        instance_pk,
        incoming_payload("S1", type_message="stickerMessage"),
        "S1",
    )
    await run_processor(db_engine)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        message = (await db.execute(select(Message))).scalar_one()
    assert message.state is MsgState.IGNORED  # never answered, never AI
    assert message.media_type is not None
    assert message.media_type.value == "sticker"


async def test_operator_phone_reply_clears_attention_with_audit(
    db_engine, instance_pk
) -> None:
    """Owner decision: a manager answering from their phone clears the flag."""
    await store(db_engine, instance_pk, incoming_payload("M1"), "M1")
    await run_processor(db_engine)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        chat = (await db.execute(select(Chat))).scalar_one()
        await set_attention(db, chat, AttentionReason.HANDOFF)

    await store(
        db_engine,
        instance_pk,
        incoming_payload(
            "OP1", text="Отвечаю лично", webhook="outgoingMessageReceived"
        ),
        "OP1",
    )
    await run_processor(db_engine)

    async with maker() as db:
        chat = (await db.execute(select(Chat))).scalar_one()
        reply = (
            await db.execute(
                select(Message).where(Message.provider_message_id == "OP1")
            )
        ).scalar_one()
        audit = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "chat.attention_cleared")
            )
        ).scalar_one()

    assert chat.needs_attention is False
    assert chat.attention_cleared_reason == "operator_phone_reply"
    assert reply.source is MsgSource.OPERATOR_PHONE
    assert audit.after == {"reason": "operator_phone_reply"}


async def test_api_echo_links_to_outbox_message(db_engine, instance_pk) -> None:
    """Our own send echoed back must NOT become a duplicate message row."""
    outbound_id = uuid.uuid4()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        chat = Chat(
            instance_id=instance_pk,
            wa_chat_id=WA_CHAT,
            phone="+77019481555",
        )
        db.add(chat)
        await db.flush()
        db.add(
            Message(
                chat_id=chat.id,
                direction="out",
                source="operator_panel",
                outbound_message_id=outbound_id,
                body="Ответ из панели",
                state="GENERATED",
            )
        )
        db.add(
            WaOutbox(
                instance_id=instance_pk,
                outbound_message_id=outbound_id,
                wa_chat_id=WA_CHAT,
                body="Ответ из панели",
                state="SENT",
                provider_message_id="ECHO1",
            )
        )

    await store(
        db_engine,
        instance_pk,
        incoming_payload(
            "ECHO1", text="Ответ из панели", webhook="outgoingAPIMessageReceived"
        ),
        "ECHO1",
    )
    await run_processor(db_engine)

    async with maker() as db:
        messages = (await db.execute(select(Message))).scalars().all()
    assert len(messages) == 1  # linked, not duplicated
    assert messages[0].provider_message_id == "ECHO1"


async def test_delivery_status_updates_message(db_engine, instance_pk) -> None:
    await store(db_engine, instance_pk, incoming_payload("M1"), "M1")
    await run_processor(db_engine)

    status_payload = {
        "typeWebhook": "outgoingMessageStatus",
        "idMessage": "M1",
        "status": "read",
        "timestamp": 1756700100,
    }
    await store(db_engine, instance_pk, status_payload, "M1:status:read")
    await run_processor(db_engine)

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        message = (await db.execute(select(Message))).scalar_one()
    assert message.delivery_status == "read"


async def test_reconciler_propagates_outbox_states(db_engine, instance_pk) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    sent_id, dry_id, failed_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with maker() as db, db.begin():
        chat = Chat(instance_id=instance_pk, wa_chat_id=WA_CHAT, phone="+77019481555")
        db.add(chat)
        await db.flush()
        for out_id, outbox_state, extra in (
            (sent_id, "SENT", {"provider_message_id": "W1"}),
            (dry_id, "SKIPPED_DRY_RUN", {}),
            (failed_id, "FAILED_FINAL", {"error_code": "provider:400"}),
        ):
            db.add(
                Message(
                    chat_id=chat.id,
                    direction="out",
                    source="operator_panel",
                    outbound_message_id=out_id,
                    body="x",
                    state="GENERATED",
                )
            )
            db.add(
                WaOutbox(
                    instance_id=instance_pk,
                    outbound_message_id=out_id,
                    wa_chat_id=WA_CHAT,
                    body="x",
                    state=outbox_state,
                    **extra,
                )
            )

    async with maker() as db:
        assert await reconcile_outbox(db) == 3
        await db.commit()

    async with maker() as db:
        by_id = {
            m.outbound_message_id: m
            for m in (await db.execute(select(Message))).scalars().all()
        }
    assert by_id[sent_id].state is MsgState.SENT
    assert by_id[sent_id].provider_message_id == "W1"
    # DRY_RUN: honestly NOT marked as sent.
    assert by_id[dry_id].state is MsgState.GENERATED
    assert by_id[dry_id].delivery_status == "skipped_dry_run"
    assert by_id[failed_id].state is MsgState.FAILED_FINAL
    assert by_id[failed_id].error_code == "provider:400"
