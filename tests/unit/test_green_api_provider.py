"""GreenApiProvider against httpx.MockTransport: request formation,
long-poll semantics, error classification, token hygiene."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.whatsapp.green_api import GreenApiProvider
from app.whatsapp.provider import ProviderError
from cryptography.fernet import Fernet

TOKEN = "supersecrettoken123"


def make_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        encryption_key=os.environ.get("ENCRYPTION_KEY", Fernet.generate_key().decode()),
        cors_origins="",
        green_api_id_instance="1101000001",
        green_api_token=TOKEN,
        _env_file=None,
    )


def make_provider(handler: Any) -> GreenApiProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport, base_url="https://api.green-api.example"
    )
    return GreenApiProvider(make_settings(), client=client)


async def test_poll_returns_none_on_empty_queue() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "receiveNotification" in request.url.path
        assert request.url.params["receiveTimeout"] == "20"
        return httpx.Response(200, content=b"null")

    provider = make_provider(handler)
    assert await provider.poll_notification() is None


async def test_poll_parses_notification() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"receiptId": 42, "body": {"typeWebhook": "incomingMessageReceived"}},
        )

    provider = make_provider(handler)
    result = await provider.poll_notification()
    assert result is not None
    assert result.receipt_id == 42
    assert result.body["typeWebhook"] == "incomingMessageReceived"


async def test_send_text_posts_chat_and_message() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"idMessage": "OUT1"})

    provider = make_provider(handler)
    result = await provider.send_text("77011234567@c.us", "Здравствуйте!")
    assert result.provider_message_id == "OUT1"
    assert seen["json"] == {"chatId": "77011234567@c.us", "message": "Здравствуйте!"}
    assert TOKEN in seen["path"]  # GREEN-API design: token in path...


async def test_errors_are_sanitized_and_classified() -> None:
    """...which is exactly why errors must never carry the URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as exc_info:
        await provider.send_text("77011234567@c.us", "hi")
    error = exc_info.value
    assert error.retryable is True
    assert error.status_code == 429
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)


async def test_auth_error_is_fatal_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as exc_info:
        await provider.get_instance_state()
    assert exc_info.value.retryable is False


async def test_network_error_is_retryable_and_tokenless() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = make_provider(handler)
    with pytest.raises(ProviderError) as exc_info:
        await provider.poll_notification()
    assert exc_info.value.retryable is True
    assert TOKEN not in str(exc_info.value)


async def test_delete_notification_uses_receipt_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"result": True})

    provider = make_provider(handler)
    await provider.delete_notification(42)
    assert seen["method"] == "DELETE"
    assert (
        seen["path"].endswith("/deleteNotification/42/" + TOKEN) or "42" in seen["path"]
    )


def test_provider_refuses_missing_configuration() -> None:
    settings = make_settings().model_copy(
        update={"green_api_id_instance": "", "green_api_token": ""}
    )
    with pytest.raises(ProviderError):
        GreenApiProvider(settings)
