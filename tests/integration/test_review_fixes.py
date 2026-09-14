# ruff: noqa: F811 - pytest fixtures are re-exported by name and then used as parameters
"""Defects found by the acceptance review (2026-09-15) and their fixes.

Each test is the failure scenario the reviewer described, made real
against PostgreSQL: a paid call that died in flight, a send the provider
refused, a number that logged out, a second factor with no lockout. If
one of these turns red again, the same customer-visible problem is back.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.chat_provider import ChatError, ChatUsage, make_result
from app.models import (
    AiCall,
    AlertNotification,
    Chat,
    InboundNotification,
    KbFaq,
    Message,
    SystemEvent,
    WaOutbox,
    WhatsappInstance,
)
from app.models.enums import (
    AnswerSource,
    AnswerStatus,
    AttentionReason,
    BudgetScope,
    CallType,
    KbLanguage,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services import analytics
from app.services.agent import activate_version, create_version
from app.services.budget import build_scopes
from app.services.inbox_processing import reconcile_outbox
from app.services.kb_faq import build_pattern, match_faq
from app.services.media_processing import enqueue_media_job
from app.services.semantic_cache import get_kb_version
from app.whatsapp.provider import RawNotification
from app.whatsapp.receiver import PollingReceiver
from app.worker.pipeline import correlation_for
from tests.conftest import TEST_PASSWORD, login, totp_code
from tests.integration.test_ai_pipeline import (  # noqa: F401 - fixtures
    ANSWER,
    add_queued,
    env,
    get_message,
    get_reply,
    instance_pk,
    make_chat,
)
from tests.integration.test_inbox import FakeProvider
from tests.integration.test_message_kinds import CHAT_ID, deliver, maker  # noqa: F401 - fixture

NUL = chr(0)

# ------------------------- деньги: барьер до оплаты -------------------------


async def test_a_chat_call_that_died_in_flight_is_never_paid_again(env) -> None:
    """The worker was killed after the model answered and before the
    accounting row existed. All that is left is the `started` barrier
    row — and that is enough: a human answers, the model is not called."""
    maker = env["maker"]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)
    correlation = correlation_for(chat_id, msg_id)
    async with maker() as db, db.begin():
        db.add(
            AiCall(
                correlation_id=correlation,
                chat_id=chat_id,
                call_type=CallType.CHAT,
                model="gpt-5.6-luna",
                status="started",
                barrier_key=f"{correlation}:main",
            )
        )

    await env["pipeline"].run_once()

    assert env["chat_provider"].requests == []
    message = await get_message(maker, msg_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "already_charged"
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
    assert chat.needs_attention


async def test_an_ambiguous_model_failure_is_not_retried_into_a_second_payment(env) -> None:
    """A timeout may have been billed. Before, the message was re-queued
    and paid for again; now the barrier stays and a human takes over."""
    maker = env["maker"]
    env["chat_provider"].script = [ChatError("chat_failed:APITimeoutError", retryable=True)]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)

    await env["pipeline"].run_once()

    message = await get_message(maker, msg_id)
    assert message.state is MsgState.FAILED_FINAL
    async with maker() as db:
        call = (
            await db.execute(select(AiCall).where(AiCall.call_type == CallType.CHAT))
        ).scalar_one()
        chat = await db.get(Chat, chat_id)
    assert call.status == "error"
    assert call.barrier_key is not None  # still blocking a second payment
    assert chat.needs_attention


async def test_a_definite_provider_error_lifts_the_barrier_for_the_retry(env) -> None:
    maker = env["maker"]
    env["chat_provider"].script = [
        ChatError("chat_failed:http_503", retryable=True),
        make_result(ANSWER, input_tokens=1000),
    ]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)

    await env["pipeline"].run_once()
    assert (await get_message(maker, msg_id)).state is MsgState.QUEUED  # will be retried
    async with maker() as db, db.begin():
        row = await db.get(Message, msg_id)
        row.next_attempt_at = None
    await env["pipeline"].run_once()

    assert (await get_message(maker, msg_id)).state is MsgState.ANSWERED
    async with maker() as db:
        calls = (
            (
                await db.execute(
                    select(AiCall).where(AiCall.call_type == CallType.CHAT).order_by(AiCall.id)
                )
            )
            .scalars()
            .all()
        )
    assert [c.status for c in calls] == ["error", "ok"]
    assert calls[0].barrier_key is None  # lifted: nothing was billed
    assert calls[1].barrier_key is not None


async def test_a_billed_empty_completion_is_the_dialogs_spend(env) -> None:
    maker = env["maker"]
    env["chat_provider"].script = [
        ChatError(
            "chat_failed:empty_response",
            retryable=True,
            usage=ChatUsage(input_tokens=1000, output_tokens=0, reasoning_tokens=100),
        )
    ]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)

    await env["pipeline"].run_once()

    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        call = (
            await db.execute(select(AiCall).where(AiCall.call_type == CallType.CHAT))
        ).scalar_one()
    # 1000 input * $0.20/1M + 100 reasoning * $1.20/1M
    assert call.cost_usd == Decimal("0.00032000")
    assert chat.total_cost_usd == Decimal("0.000320")
    assert (await get_message(maker, msg_id)).state is MsgState.FAILED_FINAL


def test_month_budget_period_ends_on_the_last_day() -> None:
    """An exclusive end (the 1st of next month) matched two month rows on
    every 1st, because all readers compare `period_end >= today`."""
    specs = {s.scope: s for s in build_scopes(None, None, datetime(2026, 9, 15, tzinfo=UTC))}
    month = specs[BudgetScope.GLOBAL_MONTH]
    assert month.period_start.isoformat() == "2026-09-01"
    assert month.period_end.isoformat() == "2026-09-30"
    december = {s.scope: s for s in build_scopes(None, None, datetime(2026, 12, 3, tzinfo=UTC))}[
        BudgetScope.GLOBAL_MONTH
    ]
    assert december.period_end.isoformat() == "2026-12-31"


# ------------------------- аналитика: честный процент -------------------------


async def test_automation_counts_only_real_answers(db_engine, instance_pk) -> None:
    """A composed «передал менеджеру» and a processing failure are not
    automation; they used to be counted as answers (or not at all)."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        chat = Chat(instance_id=instance_pk, wa_chat_id="77010009999@c.us", phone="+77010009999")
        db.add(chat)
        await db.flush()
        for correlation, source in (("c1", AnswerSource.SERVICE), ("c2", AnswerSource.MODEL)):
            db.add(
                Message(
                    chat_id=chat.id,
                    direction=MsgDirection.IN,
                    source=MsgSource.CUSTOMER,
                    body="q",
                    state=MsgState.ANSWERED,
                    correlation_id=correlation,
                )
            )
            db.add(
                Message(
                    chat_id=chat.id,
                    direction=MsgDirection.OUT,
                    source=MsgSource.AI,
                    body="a",
                    state=MsgState.GENERATED,
                    correlation_id=correlation,
                    answer_source=source,
                    outbound_message_id=uuid.uuid4(),
                )
            )
        db.add(
            Message(
                chat_id=chat.id,
                direction=MsgDirection.IN,
                source=MsgSource.CUSTOMER,
                body="q3",
                state=MsgState.FAILED_FINAL,
                correlation_id="c3",
            )
        )
    async with maker() as db:
        work = await analytics.workload(db, analytics.period_from_days(7))
    assert work["incoming"] == 3
    assert work["answered_by_ai"] == 1
    assert work["handed_to_human"] == 2
    assert work["automation_percent"] == 33


# ------------------------- база знаний -------------------------


async def test_faq_pattern_shared_by_two_languages_answers_in_the_customers(db_engine) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        russian = KbFaq(
            normalized_key="wifi-ru",
            answer_ru="Wi-Fi есть, пароль на стойке.",
            answer_ru_status=AnswerStatus.APPROVED,
            is_active=True,
        )
        kazakh = KbFaq(
            normalized_key="wifi-kk",
            answer_ru="Wi-Fi бар.",
            answer_ru_status=AnswerStatus.APPROVED,
            answer_kk="Wi-Fi бар, құпия сөз тіркеу үстелінде.",
            answer_kk_status=AnswerStatus.APPROVED,
            is_active=True,
        )
        db.add_all([russian, kazakh])
        await db.flush()
        db.add(build_pattern(russian.id, KbLanguage.RU, "wifi"))
        db.add(build_pattern(kazakh.id, KbLanguage.KK, "wifi"))
    async with maker() as db:
        kk_hit = await match_faq(db, "wifi", KbLanguage.KK)
        ru_hit = await match_faq(db, "wifi", KbLanguage.RU)
    assert kk_hit is not None and kk_hit.answer.startswith("Wi-Fi бар, құпия")
    assert ru_hit is not None and ru_hit.answer.startswith("Wi-Fi есть")


async def test_activating_an_instruction_invalidates_the_cache(db_engine) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        before = await get_kb_version(db)
        instruction = await create_version(
            db,
            system_prompt="Ты — консультант организации. Отвечай кратко.",
            style=None,
            escalation_rules=None,
            max_output_tokens=None,
            reasoning_effort=None,
            author_user_id=None,
        )
        await activate_version(db, instruction, actor_user_id=None)
    async with maker() as db:
        assert await get_kb_version(db) == before + 1


async def test_translate_refuses_to_overwrite_an_approved_translation(
    authed_client, db_engine
) -> None:
    client, headers = authed_client
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        faq = KbFaq(
            normalized_key="hours",
            answer_ru="Работаем с 07:00 до 23:00.",
            answer_ru_status=AnswerStatus.APPROVED,
            answer_kk="07:00-ден 23:00-ге дейін жұмыс істейміз.",
            answer_kk_status=AnswerStatus.APPROVED,
        )
        db.add(faq)
        await db.flush()
        faq_id = faq.id
    response = await client.post(
        f"/api/kb/faq/{faq_id}/translate", json={"language": "kk"}, headers=headers
    )
    assert response.status_code == 409


async def test_adding_a_pattern_bumps_the_knowledge_version(authed_client, db_engine) -> None:
    client, headers = authed_client
    created = await client.post(
        "/api/kb/faq",
        json={
            "answer_ru": "Есть парковка у входа.",
            "category": "org",
            "patterns_ru": ["парковка"],
            "patterns_kk": ["автотұрақ"],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db:
        before = await get_kb_version(db)
    added = await client.post(
        f"/api/kb/faq/{created.json()['id']}/patterns",
        json={"language": "ru", "pattern": "где припарковаться"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    async with maker() as db:
        assert await get_kb_version(db) == before + 1


# ------------------------- транспорт -------------------------


async def _seed_reply(
    maker: async_sessionmaker,
    instance: int,
    *,
    outbox_state: str,
    correlation: str,
    provider_message_id: str | None = None,
    delivery_status: str | None = None,
    error_code: str | None = None,
) -> tuple[int, uuid.UUID]:
    outbound_id = uuid.uuid4()
    async with maker() as db, db.begin():
        chat = Chat(instance_id=instance, wa_chat_id="77010001111@c.us", phone="+77010001111")
        db.add(chat)
        await db.flush()
        db.add(
            Message(
                chat_id=chat.id,
                direction=MsgDirection.IN,
                source=MsgSource.CUSTOMER,
                body="вопрос",
                state=MsgState.ANSWERED,
                correlation_id=correlation,
            )
        )
        db.add(
            Message(
                chat_id=chat.id,
                direction=MsgDirection.OUT,
                source=MsgSource.AI,
                outbound_message_id=outbound_id,
                body="ответ",
                state=MsgState.GENERATED,
                correlation_id=correlation,
                answer_source=AnswerSource.MODEL,
                delivery_status=delivery_status,
            )
        )
        db.add(
            WaOutbox(
                instance_id=instance,
                outbound_message_id=outbound_id,
                wa_chat_id="77010001111@c.us",
                body="ответ",
                state=outbox_state,
                correlation_id=correlation,
                provider_message_id=provider_message_id,
                error_code=error_code,
            )
        )
        return chat.id, outbound_id


async def test_a_send_the_provider_refused_is_loud(db_engine, instance_pk) -> None:
    """The reply exists, the customer never got it: the dialog goes to a
    manager, the owner is told, the question is no longer «answered»."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    chat_id, _ = await _seed_reply(
        maker,
        instance_pk,
        outbox_state="FAILED_FINAL",
        correlation="c-1",
        error_code="provider:400",
    )
    async with maker() as db:
        assert await reconcile_outbox(db) == 1
        await db.commit()
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
        inbound = (
            await db.execute(select(Message).where(Message.direction == MsgDirection.IN))
        ).scalar_one()
        alert = (
            await db.execute(
                select(AlertNotification).where(AlertNotification.event_key == "send_failed")
            )
        ).scalar_one()
    assert chat.needs_attention and chat.attention_reason is AttentionReason.PROCESSING_ERROR
    assert inbound.state is MsgState.HANDED_TO_HUMAN
    assert inbound.error_code == "send_failed"
    assert alert.kind == "system"


async def test_a_reply_whose_status_arrived_first_is_still_reconciled(
    db_engine, instance_pk
) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    _, outbound_id = await _seed_reply(
        maker,
        instance_pk,
        outbox_state="SENT",
        correlation="c-2",
        provider_message_id="P-2",
        delivery_status="delivered",  # the provider's status beat the reconciler
    )
    async with maker() as db:
        assert await reconcile_outbox(db) == 1
        await db.commit()
    async with maker() as db:
        reply = (
            await db.execute(select(Message).where(Message.outbound_message_id == outbound_id))
        ).scalar_one()
    assert reply.state is MsgState.SENT
    assert reply.provider_message_id == "P-2"
    assert reply.delivery_status == "delivered"


async def test_an_echo_that_beat_the_flusher_is_folded_into_the_reply(
    db_engine, instance_pk
) -> None:
    """The echo arrived before the flusher committed its provider id and
    became a `system` message with that id; reconciling used to violate
    the unique key and roll back the whole inbox batch — forever."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    chat_id, outbound_id = await _seed_reply(
        maker, instance_pk, outbox_state="SENT", correlation="c-3", provider_message_id="P-3"
    )
    async with maker() as db, db.begin():
        db.add(
            Message(
                chat_id=chat_id,
                direction=MsgDirection.OUT,
                source=MsgSource.SYSTEM,
                body="ответ",
                state=MsgState.SENT,
                provider_message_id="P-3",
            )
        )
    async with maker() as db:
        assert await reconcile_outbox(db) == 1
        await db.commit()
    async with maker() as db:
        rows = (
            (await db.execute(select(Message).where(Message.direction == MsgDirection.OUT)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the system duplicate is gone
    assert rows[0].outbound_message_id == outbound_id
    assert rows[0].provider_message_id == "P-3"
    assert rows[0].state is MsgState.SENT


async def test_a_logged_out_number_is_an_alert(maker) -> None:
    await deliver(maker, {"typeWebhook": "stateInstanceChanged", "stateInstance": "notAuthorized"})
    async with maker() as db:
        instance = (await db.execute(select(WhatsappInstance))).scalar_one()
        event = (
            await db.execute(
                select(SystemEvent).where(SystemEvent.event == "instance_state_changed")
            )
        ).scalar_one()
        alert = (
            await db.execute(
                select(AlertNotification).where(AlertNotification.event_key == "instance_state")
            )
        ).scalar_one()
    assert instance.state == "notAuthorized"
    assert event.level == "critical"
    assert alert.kind == "system"


async def test_yellow_card_is_reported_even_for_an_unknown_message(maker) -> None:
    await deliver(
        maker,
        {
            "typeWebhook": "outgoingMessageStatus",
            "idMessage": "nobody-knows",
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


async def test_a_failed_delivery_without_a_matching_message_still_flags_the_chat(maker) -> None:
    await deliver(
        maker,
        {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "q-1",
            "timestamp": 1789000000,
            "senderData": {"chatId": CHAT_ID, "senderName": "Клиент"},
            "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "?"}},
        },
    )
    await deliver(
        maker,
        {
            "typeWebhook": "outgoingMessageStatus",
            "idMessage": "sent-without-id",
            "status": "failed",
            "chatId": CHAT_ID,
        },
    )
    async with maker() as db:
        chat = (await db.execute(select(Chat))).scalar_one()
    assert chat.needs_attention


async def test_a_payload_the_database_rejects_is_acked_not_redelivered_forever(
    db_engine, instance_pk
) -> None:
    """A NUL character inside JSONB is refused by PostgreSQL every time:
    re-delivering it blocked every message behind it."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    poison = RawNotification(
        receipt_id=41,
        body={
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "nul-1",
            "timestamp": 1756600000,
            "senderData": {"chatId": "77011234567@c.us"},
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": f"при{NUL}вет"},
            },
        },
    )
    provider = FakeProvider([poison])
    receiver = PollingReceiver(maker, provider, instance_pk)

    assert await receiver.poll_once() is True

    assert provider.deleted == [41]
    async with maker() as db:
        rows = (await db.execute(select(InboundNotification.status))).scalars().all()
    assert rows == ["parse_failed"]


# ------------------------- панель и безопасность -------------------------


async def test_the_test_alert_button_tells_the_truth_about_dedup(authed_client) -> None:
    client, headers = authed_client
    first = await client.post("/api/system/test-alert", headers=headers)
    second = await client.post("/api/system/test-alert", headers=headers)
    assert first.json()["queued"] is True
    assert second.json()["queued"] is False
    assert second.json()["reason"] == "already_queued"


async def test_wrong_second_factor_codes_lock_the_account(client, settings, admin_factory) -> None:
    """The second factor is under the same lockout as the password, and a
    challenge burns after a few wrong codes — the password alone must not
    buy unlimited guesses against a six-digit code."""
    admin = await admin_factory()
    step1 = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    token = step1.json()["challenge_token"]
    for _ in range(settings.lockout_threshold):
        wrong = await client.post(
            "/api/auth/verify-totp", json={"challenge_token": token, "code": "000000"}
        )
        assert wrong.status_code == 401
    # The challenge is burnt: even the RIGHT code no longer works on it...
    right = await client.post(
        "/api/auth/verify-totp",
        json={"challenge_token": token, "code": totp_code(admin.totp_secret)},
    )
    assert right.status_code == 401
    # ...and the account is locked: the correct password is refused with
    # the same 401 a wrong one gets.
    locked = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    assert locked.status_code == 401


async def test_reauthentication_is_rate_limited(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    statuses = []
    for _ in range(6):
        response = await client.post(
            "/api/account/change-password",
            json={"current_password": "guess-wrong-123", "new_password": "x" * 16},
            headers={"X-CSRF-Token": csrf},
        )
        statuses.append(response.status_code)
    assert statuses[:5] == [403] * 5
    assert statuses[5] == 429


async def test_unknown_filter_values_and_forged_cursors_are_422(authed_client) -> None:
    client, _ = authed_client
    assert (await client.get("/api/chats?mode=foo")).status_code == 422
    assert (await client.get("/api/kb/documents?status=foo")).status_code == 422
    forged = base64.urlsafe_b64encode(f"{datetime.now(UTC).isoformat()}|{2**70}".encode()).decode()
    assert (await client.get(f"/api/chats?cursor={forged}")).status_code == 422


# ------------------------- медиа в конвейере -------------------------


async def test_a_document_flood_buys_one_acknowledgement_per_cooldown(env) -> None:
    maker = env["maker"]
    env["chat_provider"].script = [make_result("Файл получил, передал менеджеру.")]
    chat_id = await make_chat(maker, env["instance_pk"])
    first = await add_queued(maker, chat_id, "", media_type=MediaType.DOCUMENT)
    await env["pipeline"].run_once()
    second = await add_queued(maker, chat_id, "", media_type=MediaType.DOCUMENT)
    await env["pipeline"].run_once()

    assert len(env["chat_provider"].requests) == 1  # the second file was not paid for
    assert (await get_message(maker, first)).state is MsgState.HANDED_TO_HUMAN
    assert (await get_message(maker, second)).state is MsgState.HANDED_TO_HUMAN
    async with maker() as db:
        replies = (
            (
                await db.execute(
                    select(Message).where(
                        Message.chat_id == chat_id, Message.direction == MsgDirection.OUT
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_a_voice_note_still_waiting_on_its_job_is_not_mistaken_for_a_file(env) -> None:
    """The job was created between the pickup query and the claim: the
    message must stay QUEUED for the media loop, not be handed over as
    an unreadable file with a paid acknowledgement."""
    maker = env["maker"]
    chat_id = await make_chat(maker, env["instance_pk"])
    async with maker() as db, db.begin():
        message = Message(
            chat_id=chat_id,
            direction=MsgDirection.IN,
            source=MsgSource.CUSTOMER,
            provider_message_id="voice-1",
            media_type=MediaType.VOICE,
            state=MsgState.QUEUED,
        )
        db.add(message)
        await db.flush()
        await enqueue_media_job(
            db,
            message_id=message.id,
            chat_id=chat_id,
            media_type=MediaType.VOICE,
            download_url="https://media.green-api.com/voice.ogg",
            mime_type="audio/ogg",
        )
        msg_id = message.id

    await env["pipeline"].process_chat(chat_id)

    assert (await get_message(maker, msg_id)).state is MsgState.QUEUED
    assert env["chat_provider"].requests == []
    assert await get_reply(maker, chat_id) is None
