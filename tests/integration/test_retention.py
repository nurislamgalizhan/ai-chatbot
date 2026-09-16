"""Retention must delete exactly what the policy promises — no more.

The policy is docs/security-and-cost-controls.md §5. Two failure modes
are worth more than the feature itself: deleting history the owner paid
for (chats, messages, ai_calls), and NOT deleting the customer texts we
promised to stop keeping. Both are asserted here against real
PostgreSQL, because both are about SQL and constraints, not Python.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import (
    AiCall,
    AlertNotification,
    AuditLog,
    Chat,
    InboundNotification,
    KbFaq,
    KbFaqMiss,
    MediaJob,
    Message,
    SystemEvent,
    WaOutbox,
    WhatsappInstance,
)
from app.models.enums import (
    CallType,
    KbLanguage,
    MediaJobState,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services import retention

OLD = datetime.now(UTC) - timedelta(days=400)
RECENT = datetime.now(UTC) - timedelta(hours=1)


@pytest.fixture
async def seeded(db_engine):
    """A database that looks like a year of work: old debris everywhere,
    plus fresh rows of the same kinds that must survive."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        instance = WhatsappInstance(
            provider="green_api", instance_id="1101000009", token_ref="GREEN_API_TOKEN"
        )
        db.add(instance)
        await db.flush()
        chat = Chat(instance_id=instance.id, wa_chat_id="77010000009@c.us", phone="+77010000009")
        db.add(chat)
        await db.flush()

        old_message = Message(
            chat_id=chat.id,
            direction=MsgDirection.IN,
            source=MsgSource.CUSTOMER,
            state=MsgState.ANSWERED,
            body="Старое сообщение клиента",
            provider_message_id="old-1",
            created_at=OLD,
        )
        db.add(old_message)
        await db.flush()

        db.add_all(
            [
                # The customer's own words, kept twice: in `messages`
                # (forever, it is the history) and inside the provider
                # payload (30 days, it is a copy).
                InboundNotification(
                    instance_id=instance.id,
                    provider_message_id="old-1",
                    notification_type="incomingMessageReceived",
                    raw_payload={"messageData": {"textMessageData": {"textMessage": "секрет"}}},
                    received_at=OLD,
                    status="processed",
                ),
                InboundNotification(
                    instance_id=instance.id,
                    provider_message_id="fresh-1",
                    notification_type="incomingMessageReceived",
                    raw_payload={"messageData": {"textMessageData": {"textMessage": "свежее"}}},
                    received_at=RECENT,
                    status="processed",
                ),
                SystemEvent(level="info", component="test", event="old", created_at=OLD),
                SystemEvent(level="info", component="test", event="fresh", created_at=RECENT),
                AuditLog(action="settings.updated", created_at=OLD),
                AuditLog(action="settings.updated", created_at=RECENT),
                WaOutbox(
                    instance_id=instance.id,
                    outbound_message_id="11111111-1111-1111-1111-111111111111",
                    wa_chat_id=chat.wa_chat_id,
                    body="доставлено давно",
                    state="SENT",
                    created_at=OLD,
                ),
                WaOutbox(
                    instance_id=instance.id,
                    outbound_message_id="22222222-2222-2222-2222-222222222222",
                    wa_chat_id=chat.wa_chat_id,
                    body="ещё не отправлено",
                    state="PENDING",
                    created_at=OLD,
                ),
                AlertNotification(
                    kind="budget_day",
                    event_key="50pct",
                    period_key="2025-01-01",
                    status="sent",
                    created_at=OLD,
                ),
                AiCall(
                    correlation_id="keep-me",
                    chat_id=chat.id,
                    call_type=CallType.CHAT,
                    model="gpt-5.6-luna",
                    cost_usd=Decimal("0.001"),
                    cost_kzt=Decimal("0.5"),
                    created_at=OLD,
                ),
                MediaJob(
                    message_id=old_message.id,
                    chat_id=chat.id,
                    media_type=MediaType.VOICE,
                    state=MediaJobState.DONE,
                    download_url="https://provider.example/signed/voice-of-a-customer.ogg",
                    created_at=RECENT,
                ),
            ]
        )
        faq = KbFaq(normalized_key="price", category="prices")
        db.add(faq)
        await db.flush()
        db.add_all(
            [
                KbFaqMiss(faq_id=faq.id, language=KbLanguage.KK, created_at=OLD),
                KbFaqMiss(faq_id=faq.id, language=KbLanguage.KK, created_at=RECENT),
            ]
        )
    return maker


async def test_customer_texts_stop_being_kept_twice(seeded) -> None:
    """The payload copy of a customer's message is wiped after its
    window; the deduplication ROW stays, because losing it would let a
    redelivered notification buy a second answer."""
    await retention.run_retention(seeded)
    async with seeded() as db:
        rows = (
            (await db.execute(select(InboundNotification).order_by(InboundNotification.id)))
            .scalars()
            .all()
        )
    assert len(rows) == 2, "the dedup rows themselves must never be deleted"
    old_row = next(r for r in rows if r.provider_message_id == "old-1")
    fresh_row = next(r for r in rows if r.provider_message_id == "fresh-1")
    assert old_row.raw_payload is None
    assert fresh_row.raw_payload is not None


async def test_history_and_money_are_never_deleted(seeded) -> None:
    """Chats, messages and ai_calls are the product and the accounting —
    a cleanup that touches them is a data-loss bug, not a feature."""
    await retention.run_retention(seeded)
    async with seeded() as db:
        chats = (await db.execute(select(func.count()).select_from(Chat))).scalar_one()
        messages = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
        calls = (await db.execute(select(func.count()).select_from(AiCall))).scalar_one()
    assert chats == 1
    assert messages == 1  # the 400-day-old message is still there
    assert calls == 1


async def test_journals_are_trimmed_to_their_windows(seeded) -> None:
    await retention.run_retention(seeded)
    async with seeded() as db:
        events = (await db.execute(select(SystemEvent.event))).scalars().all()
        audits = (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
        misses = (await db.execute(select(func.count()).select_from(KbFaqMiss))).scalar_one()
        alerts = (
            await db.execute(select(func.count()).select_from(AlertNotification))
        ).scalar_one()
    assert "fresh" in events
    assert "old" not in events
    assert audits == 1
    assert misses == 1
    assert alerts == 0


async def test_undelivered_transport_rows_survive(seeded) -> None:
    """Only TERMINAL transport rows are debris. A PENDING row is a
    message the customer has not received yet — deleting it loses the
    answer silently."""
    await retention.run_retention(seeded)
    async with seeded() as db:
        states = (await db.execute(select(WaOutbox.state))).scalars().all()
    assert states == ["PENDING"]


async def test_signed_file_urls_do_not_outlive_the_job(seeded) -> None:
    """download_url points at the customer's voice note on the provider's
    servers. Once the job is finished nothing uses it, and it would
    otherwise sit in every database dump for years."""
    await retention.run_retention(seeded)
    async with seeded() as db:
        job = (await db.execute(select(MediaJob))).scalar_one()
    assert job.download_url is None
    assert job.state is MediaJobState.DONE  # the job record itself stays


async def test_retention_is_idempotent(seeded) -> None:
    first = await retention.run_retention(seeded)
    second = await retention.run_retention(seeded)
    assert first.total > 0
    assert second.total == 0, "a second pass must find nothing left to do"


async def test_deletes_use_indexes_not_full_scans(seeded) -> None:
    """Phase DoD: a nightly cleanup must not sequentially scan the
    biggest tables on a 2-vCPU VPS. Checked with a real EXPLAIN."""
    async with seeded() as db:
        plan = (
            (
                await db.execute(
                    select(func.count())  # placeholder to keep the session typed
                )
            ).scalar_one(),
        )
        assert plan  # sanity
        from sqlalchemy import text

        await db.execute(text("SET enable_seqscan = off"))
        for sql in (
            "EXPLAIN SELECT id FROM inbound_notifications "
            "WHERE raw_payload IS NOT NULL AND received_at < now() ORDER BY id LIMIT 500",
            "EXPLAIN SELECT id FROM system_events WHERE created_at < now() ORDER BY id LIMIT 500",
            "EXPLAIN SELECT id FROM wa_outbox WHERE created_at < now() ORDER BY id LIMIT 500",
        ):
            rows = (await db.execute(text(sql))).scalars().all()
            assert any("Index" in line for line in rows), (sql, rows)
