"""Notification classification: the single gate deciding what may ever
reach the AI pipeline (only INCOMING_PRIVATE) and what is ignored."""

from __future__ import annotations

from typing import Any

import pytest
from app.whatsapp.classify import IGNORED_KINDS, Kind, classify


def _incoming(chat_id: str, type_message: str = "textMessage") -> dict[str, Any]:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "ABC123",
        "timestamp": 1756600000,
        "senderData": {"chatId": chat_id, "sender": chat_id},
        "messageData": {"typeMessage": type_message},
    }


def test_private_text_is_the_only_ai_eligible_kind() -> None:
    result = classify(1, _incoming("77011234567@c.us"))
    assert result.kind is Kind.INCOMING_PRIVATE
    assert result.dedupe_key == "ABC123"
    assert result.kind not in IGNORED_KINDS


def test_group_message_is_fully_ignored() -> None:
    """Owner decision: the bot neither reads nor writes in groups."""
    result = classify(1, _incoming("120363123456789@g.us"))
    assert result.kind is Kind.INCOMING_GROUP
    assert result.kind in IGNORED_KINDS


def test_broadcast_is_ignored() -> None:
    result = classify(1, _incoming("status@broadcast"))
    assert result.kind is Kind.INCOMING_OTHER_CHAT
    assert result.kind in IGNORED_KINDS


def test_sticker_is_separated_from_private_text() -> None:
    """Owner decision: stickers get no answer and no model call — but the
    message itself is stored (kind is not in IGNORED_KINDS)."""
    result = classify(1, _incoming("77011234567@c.us", type_message="stickerMessage"))
    assert result.kind is Kind.INCOMING_STICKER
    assert result.kind not in IGNORED_KINDS


@pytest.mark.parametrize(
    ("webhook_type", "expected"),
    [
        ("outgoingMessageReceived", Kind.OUTGOING_PHONE),
        ("outgoingAPIMessageReceived", Kind.OUTGOING_API),
    ],
)
def test_outgoing_kinds_never_map_to_incoming(
    webhook_type: str, expected: Kind
) -> None:
    body = _incoming("77011234567@c.us")
    body["typeWebhook"] = webhook_type
    result = classify(1, body)
    assert result.kind is expected
    assert result.kind is not Kind.INCOMING_PRIVATE


def test_statuses_of_one_message_get_distinct_dedupe_keys() -> None:
    base = {
        "typeWebhook": "outgoingMessageStatus",
        "idMessage": "MSG1",
        "timestamp": 1756600000,
    }
    delivered = classify(1, {**base, "status": "delivered"})
    read = classify(2, {**base, "status": "read"})
    assert delivered.kind is Kind.DELIVERY_STATUS
    assert delivered.dedupe_key != read.dedupe_key


def test_calls_and_instance_events_are_terminal() -> None:
    call = classify(5, {"typeWebhook": "incomingCall", "timestamp": 1})
    state = classify(6, {"typeWebhook": "stateInstanceChanged", "timestamp": 2})
    assert call.kind is Kind.CALL
    assert call.kind in IGNORED_KINDS
    assert state.kind is Kind.INSTANCE_EVENT
    assert state.kind not in IGNORED_KINDS  # stored for the status page


def test_unknown_webhook_is_ignored_not_crashed() -> None:
    result = classify(7, {"typeWebhook": "somethingNew", "timestamp": 3})
    assert result.kind is Kind.UNKNOWN
    assert result.kind in IGNORED_KINDS


def test_redelivery_produces_identical_dedupe_key() -> None:
    body = _incoming("77011234567@c.us")
    first = classify(10, body)
    redelivered = classify(11, body)  # new receipt id, same message
    assert first.dedupe_key == redelivered.dedupe_key
