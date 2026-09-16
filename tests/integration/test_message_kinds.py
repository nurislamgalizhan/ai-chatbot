"""What WhatsApp can deliver besides plain text, and what the bot does.

Everything here arrives as an ordinary `incomingMessageReceived` and
differs only by `messageData.typeMessage`. Before these tests all of it
became "a file the bot cannot read": a 👍 on the bot's answer flagged the
dialog for a manager and paid for a sentence saying a FILE was received.

The line that matters most is between a REACTION and emoji INSIDE a
message: "Здравствуйте 😊 сколько стоит зал?" is a question and must be
answered like any other.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    AlertNotification,
    Chat,
    InboundNotification,
    Message,
    SystemEvent,
    WhatsappInstance,
)
from app.models.enums import MediaType, MsgState
from app.services.inbox_processing import process_inbound_batch
from app.whatsapp.classify import Kind, classify

CHAT_ID = "77010000021@c.us"


def notification(type_message: str, message_data: dict[str, Any], *, id_message: str) -> dict:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": id_message,
        "timestamp": 1789000000,
        "senderData": {"chatId": CHAT_ID, "senderName": "Клиент"},
        "messageData": {"typeMessage": type_message, **message_data},
    }


def text_notification(text: str, *, id_message: str) -> dict:
    return notification(
        "textMessage", {"textMessageData": {"textMessage": text}}, id_message=id_message
    )


@pytest.fixture
async def maker(db_engine):
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as db, db.begin():
        db.add(
            WhatsappInstance(
                provider="green_api", instance_id="1101000021", token_ref="GREEN_API_TOKEN"
            )
        )
    return sessionmaker


async def deliver(maker, payload: dict, *, status: str = "stored") -> None:
    async with maker() as db, db.begin():
        instance_id = (await db.execute(select(WhatsappInstance.id))).scalar_one()
        db.add(
            InboundNotification(
                instance_id=instance_id,
                provider_message_id=str(payload.get("idMessage") or payload["typeWebhook"]),
                notification_type=payload["typeWebhook"],
                raw_payload=payload,
                status=status,
            )
        )
    async with maker() as db:
        await process_inbound_batch(db)
        await db.commit()


async def messages(maker) -> list[Message]:
    async with maker() as db:
        return list((await db.execute(select(Message).order_by(Message.id))).scalars().all())


# --------------------------- emoji, two kinds ----------------------------


async def test_a_message_with_emoji_is_a_normal_question(maker) -> None:
    """THE case to not break while fixing reactions: people write with
    emoji all the time, and those messages must be read and answered."""
    await deliver(maker, text_notification("Здравствуйте 😊 сколько стоит зал?", id_message="m1"))

    rows = await messages(maker)
    assert len(rows) == 1
    assert rows[0].state is MsgState.QUEUED, "an emoji in the text must not stop the answer"
    assert rows[0].body == "Здравствуйте 😊 сколько стоит зал?"
    assert rows[0].media_type is None


async def test_a_message_that_is_only_emoji_is_still_a_message(maker) -> None:
    """Typed emoji is a textMessage. It may be a poor question, but the
    pipeline decides that — the inbox does not silently drop it."""
    await deliver(maker, text_notification("👍", id_message="m2"))

    rows = await messages(maker)
    assert rows[0].state is MsgState.QUEUED


async def test_a_reaction_is_not_a_question(maker) -> None:
    """A long-press reaction on the bot's answer: stored so the history
    is true, never answered, never paid for."""
    payload = notification(
        "reactionMessage",
        {
            "extendedTextMessageData": {"text": "👍"},
            "quotedMessage": {"stanzaId": "answer-1"},
        },
        id_message="r1",
    )
    assert classify(1, payload).kind is Kind.INCOMING_REACTION

    await deliver(maker, payload)

    rows = await messages(maker)
    assert len(rows) == 1
    assert rows[0].state is MsgState.IGNORED, "a reaction must never reach the AI"
    assert rows[0].error_code == "reaction"
    assert "👍" in (rows[0].body or "")
    async with maker() as db:
        chat = (await db.execute(select(Chat))).scalar_one()
    assert chat.needs_attention is False, "a thumbs-up is not a reason to call a manager"


# ------------------------- edits and deletions ---------------------------


async def test_an_edited_question_is_answered(maker) -> None:
    """The customer corrects their own message. The new text IS the
    question now; it used to arrive with no text at all and was answered
    as an unreadable file."""
    await deliver(
        maker,
        notification(
            "editedMessage",
            {
                "editedMessageData": {
                    "textMessage": "Сколько стоит зал в субботу?",
                    "stanzaId": "m1",
                }
            },
            id_message="e1",
        ),
    )

    rows = await messages(maker)
    assert rows[0].state is MsgState.QUEUED
    assert rows[0].body == "Сколько стоит зал в субботу?"
    assert rows[0].media_type is None


async def test_an_edit_replaces_the_question_it_corrects(maker) -> None:
    """The edit arrives with its OWN id (verified against the provider's
    documented payload), so the original stays in the queue. Answering
    both would hand the model the question twice — once in the wording
    the customer had already fixed."""
    await deliver(maker, text_notification("Сколько стоит зал в пятницу?", id_message="orig-1"))
    await deliver(
        maker,
        notification(
            "editedMessage",
            {
                "editedMessageData": {
                    "textMessage": "Сколько стоит зал в субботу?",
                    "stanzaId": "orig-1",
                }
            },
            id_message="edit-1",
        ),
    )

    rows = await messages(maker)
    assert len(rows) == 2, "both the original and the edit exist in the history"
    original = next(r for r in rows if r.provider_message_id == "orig-1")
    edited = next(r for r in rows if r.provider_message_id == "edit-1")
    assert original.state is MsgState.IGNORED
    assert original.error_code == "superseded_by_edit"
    assert edited.state is MsgState.QUEUED, "only the corrected wording is answered"
    assert edited.body == "Сколько стоит зал в субботу?"


async def test_an_edit_after_the_answer_is_a_new_question(maker) -> None:
    """If we already answered, the correction is simply the next thing
    the customer said — and the answered row is not rewritten."""
    await deliver(maker, text_notification("Сколько стоит зал?", id_message="orig-2"))
    async with maker() as db, db.begin():
        message = (await db.execute(select(Message))).scalar_one()
        message.state = MsgState.ANSWERED

    await deliver(
        maker,
        notification(
            "editedMessage",
            {"editedMessageData": {"textMessage": "А в субботу?", "stanzaId": "orig-2"}},
            id_message="edit-2",
        ),
    )

    rows = await messages(maker)
    original = next(r for r in rows if r.provider_message_id == "orig-2")
    assert original.state is MsgState.ANSWERED
    assert original.error_code is None
    assert next(r for r in rows if r.provider_message_id == "edit-2").state is MsgState.QUEUED


async def test_a_deleted_question_is_not_answered(maker) -> None:
    """Answering a question the customer retracted is worse than silence."""
    await deliver(maker, text_notification("Ой, не то написал", id_message="m3"))
    rows = await messages(maker)
    assert rows[0].state is MsgState.QUEUED

    await deliver(
        maker,
        {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "d1",
            "senderData": {"chatId": CHAT_ID},
            "messageData": {
                "typeMessage": "deletedMessage",
                "deletedMessageData": {"stanzaId": "m3"},
            },
        },
    )

    rows = await messages(maker)
    assert len(rows) == 1, "the deletion is not a new message in the history"
    assert rows[0].state is MsgState.IGNORED
    assert rows[0].error_code == "deleted_by_customer"


async def test_deleting_an_already_answered_message_changes_nothing(maker) -> None:
    """Only work that has not happened yet is cancelled: rewriting the
    state of an answered message would corrupt the reports."""
    await deliver(maker, text_notification("Спасибо!", id_message="m4"))
    async with maker() as db, db.begin():
        message = (await db.execute(select(Message))).scalar_one()
        message.state = MsgState.ANSWERED

    await deliver(
        maker,
        {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "d2",
            "senderData": {"chatId": CHAT_ID},
            "messageData": {
                "typeMessage": "deletedMessage",
                "deletedMessageData": {"stanzaId": "m4"},
            },
        },
    )

    rows = await messages(maker)
    assert rows[0].state is MsgState.ANSWERED
    assert rows[0].error_code is None


# ------------------------ non-text content -------------------------------


async def test_a_location_is_described_not_called_a_file(maker) -> None:
    """The manager opening the chat must see WHAT arrived. And the
    customer must not be told their "file" was received."""
    await deliver(
        maker,
        notification(
            "locationMessage",
            {
                "locationMessageData": {
                    "nameLocation": "Меркурий Медет",
                    "address": "Алматы, Медеуский район",
                    "latitude": 43.2,
                    "longitude": 76.9,
                }
            },
            id_message="l1",
        ),
    )

    rows = await messages(maker)
    assert rows[0].media_type is MediaType.OTHER
    assert rows[0].body is not None
    assert "геолокация" in rows[0].body
    assert "Алматы" in rows[0].body


async def test_a_contact_card_is_described(maker) -> None:
    await deliver(
        maker,
        notification(
            "contactMessage",
            {"contactMessageData": {"displayName": "Тренер Айбар"}},
            id_message="c1",
        ),
    )

    rows = await messages(maker)
    assert rows[0].body is not None
    assert "контакт" in rows[0].body
    assert "Айбар" in rows[0].body


# --------------------------- provider events -----------------------------


async def test_quota_exceeded_is_an_alert_not_a_shrug(maker) -> None:
    """The provider says the plan limit is reached and chats stop
    working. It used to land in `unknown` and nobody ever read it."""
    payload = {
        "typeWebhook": "quotaExceeded",
        "timestamp": 1789000001,
        "instanceData": {"idInstance": 1101000021},
        "quotaData": {
            "method": "correspondents",
            "used": 100,
            "total": 100,
            "status": "CORRESPONDENTS_QUOTA_EXCEEDED",
        },
    }
    assert classify(2, payload).kind is Kind.QUOTA_EXCEEDED

    await deliver(maker, payload)

    async with maker() as db:
        events = (
            (
                await db.execute(
                    select(SystemEvent).where(SystemEvent.event == "provider_quota_exceeded")
                )
            )
            .scalars()
            .all()
        )
        alerts = (
            await db.execute(select(func.count()).select_from(AlertNotification))
        ).scalar_one()
    assert len(events) == 1
    assert events[0].level == "critical"
    assert events[0].detail["used"] == 100
    assert alerts == 1, "the owner must be told: sending has stopped"


async def test_failed_delivery_calls_a_human(maker) -> None:
    """The answer was written and sent, and the customer never got it.
    Storing the status and showing nothing (the old behaviour) means
    nobody ever notices."""
    await deliver(maker, text_notification("Вопрос", id_message="m5"))
    async with maker() as db, db.begin():
        message = (await db.execute(select(Message))).scalar_one()
        message.provider_message_id = "out-1"

    await deliver(
        maker,
        {
            "typeWebhook": "outgoingMessageStatus",
            "idMessage": "out-1",
            "status": "failed",
            "description": "Message not delivered",
        },
    )

    async with maker() as db:
        chat = (await db.execute(select(Chat))).scalar_one()
        events = (
            (await db.execute(select(SystemEvent).where(SystemEvent.event == "delivery_failed")))
            .scalars()
            .all()
        )
    assert chat.needs_attention is True
    assert len(events) == 1


async def test_yellow_card_is_reported_as_a_risk_to_the_number(maker) -> None:
    """WhatsApp suspended the number for spam-like activity: losing it
    takes the whole bot down, so this is an alert, not a log line."""
    await deliver(maker, text_notification("Вопрос", id_message="m6"))
    async with maker() as db, db.begin():
        message = (await db.execute(select(Message))).scalar_one()
        message.provider_message_id = "out-2"

    await deliver(
        maker,
        {
            "typeWebhook": "outgoingMessageStatus",
            "idMessage": "out-2",
            "status": "yellowCard",
        },
    )

    async with maker() as db:
        events = (
            (await db.execute(select(SystemEvent).where(SystemEvent.event == "number_suspended")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].level == "critical"
