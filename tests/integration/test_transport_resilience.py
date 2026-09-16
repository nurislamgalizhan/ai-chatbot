"""Poison-notification resilience (owner decision, 2026-09-01).

A payload that cannot be parsed must: be preserved as a parse_failed inbox
row, be ACKed at the provider (DeleteNotification), leave the worker alive,
and NOT block the next normal notification. Only provider/infra errors skip
the ack — there a redelivery retry is justified.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.whatsapp.receiver as receiver_module
from app.models import InboundNotification, SystemEvent, WaOutbox, WhatsappInstance
from app.whatsapp.outbox import OutboxFlusher, enqueue_text
from app.whatsapp.provider import RawNotification, SendResult
from app.whatsapp.receiver import PollingReceiver
from tests.integration.test_inbox import FakeProvider, incoming
from tests.integration.test_outbox import ScriptedSender

POISON_MARKER = "__poison__"


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


def poison(receipt_id: int) -> RawNotification:
    return RawNotification(receipt_id=receipt_id, body={POISON_MARKER: True})


@pytest.fixture
def crashing_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a parser bug: classify blows up on the poison payload."""
    original = receiver_module.classify

    def _classify(receipt_id: int, body: dict[str, Any]):
        if POISON_MARKER in body:
            msg = "simulated parser bug"
            raise RuntimeError(msg)
        return original(receipt_id, body)

    monkeypatch.setattr(receiver_module, "classify", _classify)


async def test_poison_payload_is_acked_and_does_not_wedge_the_queue(
    db_engine, instance_pk, crashing_classify
) -> None:
    """The owner-specified scenario, end to end:
    poison -> parse_failed row + delete called + worker alive,
    then the NEXT normal notification is processed as usual."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    provider = FakeProvider([poison(7), incoming(8, "GOOD-1")])
    receiver = PollingReceiver(maker, provider, instance_pk)

    # Poison: poll_once must NOT raise — the worker stays alive.
    assert await receiver.poll_once() is True
    # ...and the notification was ACKed despite the parse failure.
    assert provider.deleted == [7]

    # The queue is not wedged: the next, normal notification goes through.
    assert await receiver.poll_once() is True
    assert provider.deleted == [7, 8]

    async with maker() as db:
        rows = (
            (await db.execute(select(InboundNotification).order_by(InboundNotification.id)))
            .scalars()
            .all()
        )
        events = (
            (
                await db.execute(
                    select(SystemEvent).where(SystemEvent.event == "inbound_parse_failed")
                )
            )
            .scalars()
            .all()
        )

    poison_row = next(r for r in rows if r.status == "parse_failed")
    good_row = next(r for r in rows if r.status == "stored")
    # The raw payload is preserved for debugging (in its retention-covered home).
    assert poison_row.raw_payload == {POISON_MARKER: True}
    assert poison_row.notification_type == "parse_failed"
    assert good_row.provider_message_id == "GOOD-1"
    # A PII-free system event points at the inbox row.
    assert len(events) == 1
    assert events[0].detail["inbound_id"] == poison_row.id
    assert "textMessage" not in str(events[0].detail)


async def test_provider_error_still_skips_the_ack(db_engine, instance_pk) -> None:
    """Contrast case: transport failures keep the old retry semantics."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    provider = FakeProvider([incoming(9, "MSG-9")])
    provider.fail_delete = True
    receiver = PollingReceiver(maker, provider, instance_pk)

    from app.whatsapp.provider import ProviderError

    with pytest.raises(ProviderError):
        await receiver.poll_once()
    assert provider.deleted == []  # no ack on transport failure


async def test_outbox_row_bug_does_not_roll_back_sent_neighbours(
    db_engine, instance_pk, settings
) -> None:
    """One poison outbox row must not roll back the batch: an earlier row
    already SENT at the provider would be re-sent to the customer."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()
    async with maker() as db, db.begin():
        await enqueue_text(db, instance_pk, "77011@c.us", "ok", good_id)
        await enqueue_text(db, instance_pk, "77012@c.us", "boom", bad_id)

    provider = ScriptedSender(["WAMID-OK", RuntimeError("bug in send path")])
    live = settings.model_copy(update={"dry_run": False})
    flusher = OutboxFlusher(maker, provider, live)

    await flusher.flush_once()  # must not raise

    async with maker() as db:
        by_id = {
            r.outbound_message_id: r for r in (await db.execute(select(WaOutbox))).scalars().all()
        }
    assert by_id[good_id].state == "SENT"  # survived, will NOT be re-sent
    assert by_id[bad_id].state == "FAILED_FINAL"
    assert by_id[bad_id].error_code == "unexpected:RuntimeError"

    # Second flush finds nothing to do: no duplicates possible.
    assert await flusher.flush_once() == 0
    assert provider.sent == [("77011@c.us", "ok")]


async def test_send_without_id_message_is_sent_not_retried(
    db_engine, instance_pk, settings
) -> None:
    """HTTP 200 without idMessage = accepted: recording it as SENT with an
    unknown id beats a retry that would deliver the message twice."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    message_id = uuid.uuid4()
    async with maker() as db, db.begin():
        await enqueue_text(db, instance_pk, "77013@c.us", "hello", message_id)

    class NoIdSender(ScriptedSender):
        async def send_text(  # noqa: PLR0913 - mirrors the provider interface
            self,
            chat_id: str,
            text: str,
            *,
            quoted_message_id: str | None = None,
            typing_time_ms: int | None = None,
            link_preview: bool = True,
        ) -> SendResult:
            self.sent.append((chat_id, text))
            return SendResult(provider_message_id=None)

    provider = NoIdSender([])
    live = settings.model_copy(update={"dry_run": False})
    await OutboxFlusher(maker, provider, live).flush_once()

    async with maker() as db:
        row = (
            await db.execute(select(WaOutbox).where(WaOutbox.outbound_message_id == message_id))
        ).scalar_one()
    assert row.state == "SENT"
    assert row.provider_message_id is None
    assert len(provider.sent) == 1
