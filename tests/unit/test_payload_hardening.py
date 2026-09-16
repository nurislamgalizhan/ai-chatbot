"""Corrupted external payloads must degrade, never raise.

Covers the audit finding: direct key indexing of provider payloads. Every
nested part of a GREEN-API payload may be of the wrong type."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.wa_payload import parse_message
from app.whatsapp.classify import Kind, classify
from app.whatsapp.provider import ProviderError
from tests.unit.test_green_api_provider import make_provider


@pytest.mark.parametrize(
    "body",
    [
        {"typeWebhook": "incomingMessageReceived", "senderData": "corrupted-string"},
        {"typeWebhook": "incomingMessageReceived", "senderData": None},
        {"typeWebhook": "incomingMessageReceived", "messageData": 42},
        {"typeWebhook": "incomingMessageReceived", "senderData": ["a", "list"]},
        {"typeWebhook": 12345},
        {"_unparsed": "b'\\x00garbage'"},
        {},
    ],
)
def test_classify_never_raises_on_corrupted_payload(body: dict[str, Any]) -> None:
    result = classify(1, body)
    assert result.kind in Kind
    assert result.dedupe_key  # always non-empty: dedupe still works


@pytest.mark.parametrize(
    "body",
    [
        {"messageData": "not-a-dict"},
        {"messageData": {"typeMessage": "textMessage", "textMessageData": "oops"}},
        {"messageData": {"typeMessage": "imageMessage", "fileMessageData": []}},
        {"senderData": 0, "timestamp": "not-an-int"},
        {},
    ],
)
def test_parse_message_never_raises_on_corrupted_payload(body: dict[str, Any]) -> None:
    parsed = parse_message(body)
    assert parsed.text is None or isinstance(parsed.text, str)


async def test_envelope_without_receipt_id_is_provider_error() -> None:
    """No receiptId = nothing to ack = protocol-level retryable error,
    NOT an unhandled KeyError that kills the worker."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as exc_info:
        await provider.poll_notification()
    assert exc_info.value.retryable is True


async def test_envelope_with_missing_body_still_ackable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"receiptId": 5})

    provider = make_provider(handler)
    result = await provider.poll_notification()
    assert result is not None
    assert result.receipt_id == 5
    assert result.body == {}


async def test_envelope_with_non_dict_body_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"receiptId": 6, "body": "plain string"})

    provider = make_provider(handler)
    result = await provider.poll_notification()
    assert result is not None
    assert result.body == {"_unparsed": "'plain string'"}


async def test_send_response_without_id_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"something": "else"})

    provider = make_provider(handler)
    result = await provider.send_text("77011234567@c.us", "hi")
    assert result.provider_message_id is None


async def test_instance_state_with_odd_payload_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"weird": True})

    provider = make_provider(handler)
    assert await provider.get_instance_state() == "unknown"
