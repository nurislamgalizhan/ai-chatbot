"""Outbox: idempotent enqueue, DRY_RUN, retries with backoff, final failure.
Against real PostgreSQL — the UNIQUE constraint is the guarantee."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import SystemEvent, WaOutbox, WhatsappInstance
from app.whatsapp.outbox import OutboxFlusher, enqueue_text
from app.whatsapp.provider import ProviderError, SendResult, WhatsAppProvider


class ScriptedSender(WhatsAppProvider):
    """send_text follows a script of outcomes; everything else is inert."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[str, str]] = []

    async def poll_notification(self) -> None:
        return None

    async def delete_notification(self, receipt_id: int) -> None:
        pass

    async def send_text(  # noqa: PLR0913 - mirrors the provider interface
        self,
        chat_id: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
        typing_time_ms: int | None = None,
        link_preview: bool = True,
    ) -> SendResult:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append((chat_id, text))
        return SendResult(provider_message_id=str(outcome))

    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult:
        raise AssertionError("not used in these tests")

    async def get_instance_state(self) -> str:
        return "authorized"

    async def aclose(self) -> None:
        pass


def retryable_error() -> ProviderError:
    return ProviderError("sendMessage", status_code=503, retryable=True)


def fatal_error() -> ProviderError:
    return ProviderError("sendMessage", status_code=400, retryable=False)


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


async def _enqueue(db_engine, instance_pk: int, message_id: uuid.UUID) -> bool:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        return await enqueue_text(
            db, instance_pk, "77011234567@c.us", "Здравствуйте!", message_id, "corr-1"
        )


async def _get_row(db_engine, message_id: uuid.UUID) -> WaOutbox:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        return (
            await db.execute(select(WaOutbox).where(WaOutbox.outbound_message_id == message_id))
        ).scalar_one()


async def _make_due_now(db_engine, message_id: uuid.UUID) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(
            text("UPDATE wa_outbox SET next_attempt_at = now() WHERE outbound_message_id = :mid"),
            {"mid": str(message_id)},
        )


async def test_enqueue_is_idempotent(db_engine, instance_pk) -> None:
    message_id = uuid.uuid4()
    assert await _enqueue(db_engine, instance_pk, message_id) is True
    assert await _enqueue(db_engine, instance_pk, message_id) is False

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        count = len((await db.execute(select(WaOutbox.id))).scalars().all())
    assert count == 1


async def test_dry_run_never_calls_provider(db_engine, instance_pk, settings) -> None:
    """Acceptance mode (SPEC §17): processed and logged, NOT sent."""
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    provider = ScriptedSender(["SHOULD-NOT-BE-USED"])
    dry_settings = settings.model_copy(update={"dry_run": True})
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    handled = await OutboxFlusher(maker, provider, dry_settings).flush_once()

    assert handled == 1
    assert provider.sent == []
    row = await _get_row(db_engine, message_id)
    assert row.state == "SKIPPED_DRY_RUN"


async def test_successful_send_records_provider_id(db_engine, instance_pk, settings) -> None:
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    provider = ScriptedSender(["WAMID1"])
    live = settings.model_copy(update={"dry_run": False})
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    await OutboxFlusher(maker, provider, live).flush_once()

    row = await _get_row(db_engine, message_id)
    assert row.state == "SENT"
    assert row.provider_message_id == "WAMID1"
    assert row.sent_at is not None
    assert provider.sent == [("77011234567@c.us", "Здравствуйте!")]


async def test_retry_reuses_same_text_and_never_duplicates(
    db_engine, instance_pk, settings
) -> None:
    """Transient failure -> retry sends THE SAME queued text exactly once."""
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    provider = ScriptedSender([retryable_error(), "WAMID2"])
    live = settings.model_copy(update={"dry_run": False})
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    flusher = OutboxFlusher(maker, provider, live)

    await flusher.flush_once()
    row = await _get_row(db_engine, message_id)
    assert row.state == "FAILED_RETRYABLE"
    assert row.retry_count == 1
    assert row.next_attempt_at > datetime.now(UTC)  # backoff scheduled

    await _make_due_now(db_engine, message_id)
    await flusher.flush_once()

    row = await _get_row(db_engine, message_id)
    assert row.state == "SENT"
    assert provider.sent == [("77011234567@c.us", "Здравствуйте!")]  # exactly once


async def test_fatal_error_finalizes_immediately_with_system_event(
    db_engine, instance_pk, settings
) -> None:
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    provider = ScriptedSender([fatal_error()])
    live = settings.model_copy(update={"dry_run": False})
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    await OutboxFlusher(maker, provider, live).flush_once()

    row = await _get_row(db_engine, message_id)
    assert row.state == "FAILED_FINAL"
    async with maker() as db:
        event = (
            await db.execute(select(SystemEvent).where(SystemEvent.event == "send_failed_final"))
        ).scalar_one()
    assert event.level == "error"


async def test_retries_are_bounded(db_engine, instance_pk, settings) -> None:
    """retry_max_attempts transient failures -> FAILED_FINAL, no infinite loop."""
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    live = settings.model_copy(update={"dry_run": False, "retry_max_attempts": 3})
    provider = ScriptedSender([retryable_error(), retryable_error(), retryable_error()])
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    flusher = OutboxFlusher(maker, provider, live)

    for _ in range(3):
        await _make_due_now(db_engine, message_id)
        await flusher.flush_once()

    row = await _get_row(db_engine, message_id)
    assert row.state == "FAILED_FINAL"
    assert row.retry_count == 3
    assert provider.outcomes == []  # all three attempts consumed, no fourth


async def test_no_provider_configured_fails_final_not_crash(
    db_engine, instance_pk, settings
) -> None:
    message_id = uuid.uuid4()
    await _enqueue(db_engine, instance_pk, message_id)
    live = settings.model_copy(update={"dry_run": False})
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    await OutboxFlusher(maker, None, live).flush_once()

    row = await _get_row(db_engine, message_id)
    assert row.state == "FAILED_FINAL"
    assert row.error_code == "provider_not_configured"
