"""Inbox semantics against real PostgreSQL: dedupe constraint, the
store -> commit -> delete ordering, ignored kinds."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import InboundNotification, WhatsappInstance
from app.whatsapp.provider import (
    ProviderError,
    RawNotification,
    SendResult,
    WhatsAppProvider,
)
from app.whatsapp.receiver import PollingReceiver, store_notification


class FakeProvider(WhatsAppProvider):
    """Scripted provider: a queue of notifications + a call journal."""

    def __init__(self, queue: list[RawNotification]) -> None:
        self.queue = list(queue)
        self.deleted: list[int] = []
        self.fail_delete = False

    async def poll_notification(self) -> RawNotification | None:
        return self.queue.pop(0) if self.queue else None

    async def delete_notification(self, receipt_id: int) -> None:
        if self.fail_delete:
            raise ProviderError("deleteNotification", status_code=502, retryable=True)
        self.deleted.append(receipt_id)

    async def send_text(self, chat_id: str, text: str) -> SendResult:
        raise AssertionError("transport receiver must never send")

    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult:
        raise AssertionError("transport receiver must never send")

    async def get_instance_state(self) -> str:
        return "authorized"

    async def aclose(self) -> None:
        pass


def incoming(
    receipt_id: int, id_message: str, chat_id: str = "77011234567@c.us"
) -> RawNotification:
    return RawNotification(
        receipt_id=receipt_id,
        body={
            "typeWebhook": "incomingMessageReceived",
            "idMessage": id_message,
            "timestamp": 1756600000,
            "senderData": {"chatId": chat_id},
            "messageData": {"typeMessage": "textMessage"},
        },
    )


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


async def test_duplicate_notification_is_stored_once(db_engine, instance_pk) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        assert await store_notification(db, instance_pk, incoming(1, "MSG1")) is True
        # Redelivery: different receipt id, same message id.
        assert await store_notification(db, instance_pk, incoming(2, "MSG1")) is False

    async with maker() as db:
        rows = (
            (
                await db.execute(
                    select(InboundNotification).where(
                        InboundNotification.provider_message_id == "MSG1"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_group_notification_is_terminal_ignored(db_engine, instance_pk) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await store_notification(db, instance_pk, incoming(1, "GRP1", chat_id="1203630000@g.us"))
    async with maker() as db:
        row = (
            await db.execute(
                select(InboundNotification).where(InboundNotification.provider_message_id == "GRP1")
            )
        ).scalar_one()
    assert row.status == "ignored"
    assert row.processed_at is not None


async def test_poll_once_stores_then_deletes(db_engine, instance_pk) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    provider = FakeProvider([incoming(7, "MSG7")])
    receiver = PollingReceiver(maker, provider, instance_pk)

    assert await receiver.poll_once() is True
    assert provider.deleted == [7]

    async with maker() as db:
        row = (
            await db.execute(
                select(InboundNotification).where(InboundNotification.provider_message_id == "MSG7")
            )
        ).scalar_one()
    assert row.status == "stored"


async def test_failed_delete_redelivery_does_not_duplicate(db_engine, instance_pk) -> None:
    """delete_notification fails AFTER our commit -> the provider redelivers
    -> the unique constraint absorbs it and delete is attempted again."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    provider = FakeProvider([incoming(7, "MSG7")])
    provider.fail_delete = True
    receiver = PollingReceiver(maker, provider, instance_pk)

    with pytest.raises(ProviderError):
        await receiver.poll_once()

    # Redelivery of the same notification with a new receipt id.
    provider.fail_delete = False
    provider.queue = [incoming(8, "MSG7")]
    assert await receiver.poll_once() is True
    assert provider.deleted == [8]

    async with maker() as db:
        count = len(
            (
                await db.execute(
                    select(InboundNotification).where(
                        InboundNotification.provider_message_id == "MSG7"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert count == 1


async def test_empty_queue_returns_false(db_engine, instance_pk) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    receiver = PollingReceiver(maker, FakeProvider([]), instance_pk)
    assert await receiver.poll_once() is False
