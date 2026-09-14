"""Alerts are the only thing that tells the owner the bot went quiet.

What is asserted here is exactly what can go wrong with them: saying the
same thing twice, saying nothing because acceptance mode muted it,
answering the owner's own reply as if they were a customer, and charging
money for any of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.config import Settings
from app.models import AlertNotification, BudgetLedger, Chat, Message, WaOutbox, WhatsappInstance
from app.models.enums import BudgetScope
from app.services import app_settings, budget
from app.services.alerts import (
    AlertDispatcher,
    alert_text,
    hour_bucket,
    is_alert_chat,
    record_alert,
    wa_chat_id_for,
)
from app.services.app_settings import set_setting
from app.whatsapp.outbox import ALERT_PURPOSE, OutboxFlusher
from app.whatsapp.provider import ProviderError, SendResult, WhatsAppProvider
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

OWNER_PHONE = "+77015550001"


class FakeProvider(WhatsAppProvider):
    """Records sends instead of performing them."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_times = fail_times

    async def poll_notification(self):  # pragma: no cover - unused here
        return None

    async def delete_notification(self, receipt_id: int) -> None:  # pragma: no cover
        return None

    async def send_text(  # noqa: PLR0913 - mirrors the provider interface
        self,
        chat_id: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
        typing_time_ms: int | None = None,
        link_preview: bool = True,
    ) -> SendResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError("sendMessage", status_code=500, retryable=True)
        self.sent.append((chat_id, text))
        return SendResult(provider_message_id=f"wamid-{len(self.sent)}")

    async def send_file(self, *args, **kwargs) -> SendResult:  # pragma: no cover
        raise NotImplementedError

    async def set_typing(self, chat_id: str) -> None:  # pragma: no cover
        return None

    async def download_file(self, url: str, max_bytes: int) -> bytes:  # pragma: no cover
        raise NotImplementedError

    async def get_instance_state(self) -> str:  # pragma: no cover
        return "authorized"

    async def aclose(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
async def maker(db_engine):
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as db, db.begin():
        db.add(
            WhatsappInstance(
                provider="green_api", instance_id="1101000011", token_ref="GREEN_API_TOKEN"
            )
        )
        await set_setting(db, app_settings.ALERT_PHONE, OWNER_PHONE)
    return sessionmaker


def dry_run_settings(database_url: str) -> Settings:
    """Acceptance mode: the state this system lives in (CLAUDE.md §2.12)."""
    import os

    return Settings(
        app_env="test",
        database_url=database_url,
        redis_url="redis://unused:6379/0",
        encryption_key=os.environ["ENCRYPTION_KEY"],
        cors_origins="",
        dry_run=True,
    )


async def test_owner_hears_about_a_spent_budget_even_in_dry_run(maker, migrated_db) -> None:
    """THE point of the phase. DRY_RUN silences messages to CUSTOMERS;
    silencing 'your budget is gone and the bot stopped answering' would
    mean the owner discovers it from a customer complaint."""
    async with maker() as db:
        await record_alert(
            db,
            kind="budget_day",
            event_key="100pct",
            period_key="2026-09-14",
            detail={"spent_kzt": "300", "limit_kzt": "300"},
        )
        await db.commit()

    await AlertDispatcher(maker).dispatch_pending()
    provider = FakeProvider()
    await OutboxFlusher(maker, provider, dry_run_settings(migrated_db)).flush_once()

    assert len(provider.sent) == 1
    chat_id, text = provider.sent[0]
    assert chat_id == wa_chat_id_for(OWNER_PHONE)
    assert "Дневной бюджет" in text
    assert "300" in text  # tenge, the currency the owner sets limits in

    async with maker() as db:
        await AlertDispatcher(maker).dispatch_pending()  # settle the status
        row = (await db.execute(select(AlertNotification))).scalar_one()
    assert row.status == "sent"
    assert row.sent_at is not None


async def test_customer_messages_stay_muted_by_dry_run(maker, migrated_db) -> None:
    """The exemption is for alerts only; the acceptance-mode promise to
    customers must hold exactly as before."""
    async with maker() as db:
        instance_id = (await db.execute(select(WhatsappInstance.id))).scalar_one()
        db.add(
            WaOutbox(
                instance_id=instance_id,
                outbound_message_id="33333333-3333-3333-3333-333333333333",
                wa_chat_id="77019999999@c.us",
                body="Ответ клиенту",
            )
        )
        await db.commit()

    provider = FakeProvider()
    await OutboxFlusher(maker, provider, dry_run_settings(migrated_db)).flush_once()

    assert provider.sent == []
    async with maker() as db:
        row = (await db.execute(select(WaOutbox))).scalar_one()
    assert row.state == "SKIPPED_DRY_RUN"


async def test_the_same_threshold_is_never_announced_twice(maker) -> None:
    """Dedup lives in a UNIQUE constraint, so a worker restart (or two
    workers) cannot repeat it."""
    async with maker() as db:
        first = await record_alert(
            db, kind="budget_day", event_key="80pct", period_key="2026-09-14"
        )
        second = await record_alert(
            db, kind="budget_day", event_key="80pct", period_key="2026-09-14"
        )
        # ...but the same threshold in the NEXT period is a new alert.
        next_day = await record_alert(
            db, kind="budget_day", event_key="80pct", period_key="2026-09-15"
        )
        await db.commit()
        total = (await db.execute(select(func.count()).select_from(AlertNotification))).scalar_one()
    assert first is True
    assert second is False
    assert next_day is True
    assert total == 2


async def test_a_failed_alert_is_retried_then_gives_up_loudly(maker, migrated_db) -> None:
    async with maker() as db:
        await record_alert(db, kind="system", event_key="disk", period_key=hour_bucket())
        await db.commit()
    await AlertDispatcher(maker).dispatch_pending()

    settings = dry_run_settings(migrated_db)
    provider = FakeProvider(fail_times=1)
    flusher = OutboxFlusher(maker, provider, settings)
    await flusher.flush_once()

    async with maker() as db:
        row = (await db.execute(select(WaOutbox))).scalar_one()
    assert row.state == "FAILED_RETRYABLE"
    assert row.retry_count == 1

    # The retry is due later; make it due now and let it succeed.
    async with maker() as db:
        row = (await db.execute(select(WaOutbox))).scalar_one()
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    await flusher.flush_once()
    assert len(provider.sent) == 1

    await AlertDispatcher(maker).dispatch_pending()
    async with maker() as db:
        alert = (await db.execute(select(AlertNotification))).scalar_one()
    assert alert.status == "sent"


async def test_without_a_phone_nothing_is_sent_anywhere(db_engine) -> None:
    """Default state of a fresh install: the owner has not opted in, so
    alerts accumulate and are visible in «Система» — and no WhatsApp
    message goes to a number nobody confirmed."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(
            WhatsappInstance(
                provider="green_api", instance_id="1101000012", token_ref="GREEN_API_TOKEN"
            )
        )
        await record_alert(db, kind="system", event_key="disk", period_key=hour_bucket())

    result = await AlertDispatcher(maker).dispatch_pending()
    async with maker() as db:
        queued = (await db.execute(select(func.count()).select_from(WaOutbox))).scalar_one()
        alert = (await db.execute(select(AlertNotification))).scalar_one()
    assert result.skipped == 0
    assert queued == 0, "nothing may be sent to a number the owner never gave"
    # Still PENDING on purpose: the dedup is a UNIQUE constraint, so
    # retiring it now would mean this alert can never be raised again for
    # this period — the owner would add their number and never hear about
    # what already happened.
    assert alert.status == "pending"
    assert alert.last_error == "no_phone"


async def test_an_alert_that_waited_too_long_for_a_number_is_retired(db_engine) -> None:
    """...but it does not pile up forever: after the grace window an
    undeliverable alert is closed with its reason recorded."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(
            WhatsappInstance(
                provider="green_api", instance_id="1101000014", token_ref="GREEN_API_TOKEN"
            )
        )
        await record_alert(db, kind="system", event_key="disk", period_key=hour_bucket())
    async with maker() as db, db.begin():
        row = (await db.execute(select(AlertNotification))).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(days=4)

    result = await AlertDispatcher(maker).dispatch_pending()
    async with maker() as db:
        alert = (await db.execute(select(AlertNotification))).scalar_one()
    assert result.skipped == 1
    assert alert.status == "skipped"
    assert alert.last_error == "no_phone"


async def test_the_owner_is_not_a_customer(maker) -> None:
    """Our own alert echoes back from the provider, and the owner may
    reply to it. Neither may create a chat or reach the AI pipeline."""
    assert is_alert_chat(wa_chat_id_for(OWNER_PHONE), OWNER_PHONE) is True
    assert is_alert_chat("77019999999@c.us", OWNER_PHONE) is False
    assert is_alert_chat(wa_chat_id_for(OWNER_PHONE), "") is False

    from app.services.inbox_processing import process_inbound_batch
    from app.models import InboundNotification

    async with maker() as db:
        instance_id = (await db.execute(select(WhatsappInstance.id))).scalar_one()
        db.add(
            InboundNotification(
                instance_id=instance_id,
                provider_message_id="owner-reply-1",
                notification_type="incomingMessageReceived",
                raw_payload={
                    "typeWebhook": "incomingMessageReceived",
                    "idMessage": "owner-reply-1",
                    "senderData": {"chatId": wa_chat_id_for(OWNER_PHONE)},
                    "messageData": {
                        "typeMessage": "textMessage",
                        "textMessageData": {"textMessage": "ок, понял"},
                    },
                },
                status="stored",
            )
        )
        await db.commit()

        await process_inbound_batch(db)
        await db.commit()

        chats = (await db.execute(select(func.count()).select_from(Chat))).scalar_one()
        messages = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
    assert chats == 0, "the owner's own number must never become a customer chat"
    assert messages == 0


async def test_a_refused_reservation_alerts_even_below_the_thresholds(maker) -> None:
    """The bot stops answering when a reservation is REFUSED, which can
    happen at 94% spent because the next estimate no longer fits. Waiting
    for a 100% threshold that never arrives would leave the owner
    wondering why the bot went silent."""
    today = datetime.now(UTC).date()
    async with maker() as db, db.begin():
        from app.models import FxRate

        db.add(FxRate(pair="USD/KZT", rate=Decimal("500"), effective_from=datetime.now(UTC)))
        db.add(
            BudgetLedger(
                scope=BudgetScope.GLOBAL_DAY,
                scope_key=today.isoformat(),
                limit_usd=Decimal("0.010"),
                spent_usd=Decimal("0.0094"),
                reserved_usd=Decimal(0),
                period_start=today,
                period_end=today,
            )
        )

    outcome = await budget.reserve(
        maker,
        sub_correlation="chat:1:msg:1:main:a0",
        estimate_usd=Decimal("0.0008"),
        chat_id=None,
        phone=None,
    )
    assert outcome.allowed is False

    async with maker() as db:
        alerts = (await db.execute(select(AlertNotification))).scalars().all()
    assert [a.event_key for a in alerts] == ["global_day"]
    assert "лимит расходов" in (alerts[0].body or "").lower()
    assert "менеджер" in (alerts[0].body or "").lower()


def test_alert_text_never_leaks_customer_data() -> None:
    """An alert is about the SYSTEM. It goes to a phone, and it must not
    carry a customer's words or number even when the detail does."""
    text = alert_text("system", "queue", {"depth": 120, "phone": "+77019481555"})
    assert "+77019481555" not in text
    assert "120" in text
