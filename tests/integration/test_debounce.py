# ruff: noqa: F811 - pytest fixtures are re-exported by name and then used as parameters
"""The pause before answering (owner request 2026-09-17, «Время ожидания»).

The window itself is old; what is new is that the owner may now set it up
to a minute, and a minute changes what the pipeline has to survive. Every
test here is one of those consequences: a sliding window that never
closes, a batch that trips the spam gate into silence, two free FAQ
questions that become one paid call when merged, a file receipt that
waits out a pause it has no business waiting out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.chat_provider import make_result
from app.ai.guardrails import MAX_QUESTION_CHARS
from app.models import Chat, KbFaq, Message
from app.models.enums import (
    AnswerStatus,
    AttentionReason,
    KbLanguage,
    MediaType,
    MsgState,
)
from app.services import analytics, app_settings, monitoring
from app.services.app_settings import set_setting
from app.services.kb_faq import build_pattern
from app.worker.pipeline import merged_parts
from tests.integration.test_ai_pipeline import (  # noqa: F401 - fixtures
    ANSWER,
    QUESTION,
    add_queued,
    env,
    get_message,
    get_reply,
    instance_pk,
    make_chat,
)

ADDRESS_ANSWER = "Мы находимся по адресу: Коперника 130."
HOURS_ANSWER = "Работаем ежедневно с 07:00 до 23:00."


async def set_debounce(maker: async_sessionmaker, seconds: int) -> None:
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.DEBOUNCE_SECONDS, seconds)


async def age_message(maker: async_sessionmaker, message_id: int, *, seconds: int) -> None:
    """Move one message back in time — the window is measured on
    `created_at`, and a test cannot wait out a real minute."""
    async with maker() as db, db.begin():
        message = await db.get(Message, message_id)
        message.created_at = datetime.now(UTC) - timedelta(seconds=seconds)


# ------------------------------ окно ------------------------------------


async def test_the_window_holds_a_fresh_message(env) -> None:
    maker = env["maker"]
    await set_debounce(maker, 30)
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id)

    assert await env["pipeline"].run_once() == 0

    assert (await get_message(maker, msg_id)).state is MsgState.QUEUED
    assert env["chat_provider"].requests == []


async def test_the_window_closes_and_the_batch_is_answered_once(env) -> None:
    maker = env["maker"]
    await set_debounce(maker, 30)
    chat_id = await make_chat(maker, env["instance_pk"])
    first = await add_queued(maker, chat_id, "Здравствуйте")
    second = await add_queued(maker, chat_id, QUESTION)
    for message_id in (first, second):
        await age_message(maker, message_id, seconds=40)

    assert await env["pipeline"].run_once() == 1

    assert len(env["chat_provider"].requests) == 1  # обе реплики одним вопросом
    assert (await get_message(maker, first)).state is MsgState.ANSWERED
    assert (await get_message(maker, second)).state is MsgState.ANSWERED
    replies = env["chat_provider"].requests[0].messages[-1]["content"]
    assert "Здравствуйте" in replies and QUESTION in replies


async def test_a_new_message_restarts_the_window(env) -> None:
    """The pause is measured from the LAST message: a customer who is
    still typing must not be interrupted mid-thought."""
    maker = env["maker"]
    await set_debounce(maker, 30)
    chat_id = await make_chat(maker, env["instance_pk"])
    old = await add_queued(maker, chat_id, "Здравствуйте")
    await age_message(maker, old, seconds=40)
    await add_queued(maker, chat_id, QUESTION)  # только что

    assert await env["pipeline"].run_once() == 0
    assert env["chat_provider"].requests == []


async def test_a_customer_who_never_stops_typing_is_still_answered(env) -> None:
    """The ceiling. With a 60-second window, someone writing every 50
    seconds would otherwise never get an answer at all."""
    maker = env["maker"]
    await set_debounce(maker, 60)
    chat_id = await make_chat(maker, env["instance_pk"])
    oldest = await add_queued(maker, chat_id, "Здравствуйте")
    await age_message(maker, oldest, seconds=150)  # дольше потолка в 120 с
    await add_queued(maker, chat_id, QUESTION)  # и продолжает писать

    assert await env["pipeline"].run_once() == 1

    assert len(env["chat_provider"].requests) == 1
    assert (await get_message(maker, oldest)).state is MsgState.ANSWERED


async def test_a_file_does_not_wait_out_the_pause(env) -> None:
    """A document never reaches the model: it buys a receipt and a
    manager. Making it sit through a minute of debounce delays the
    handoff for nothing."""
    maker = env["maker"]
    await set_debounce(maker, 60)
    env["chat_provider"].script = [make_result("Файл получил, передал менеджеру.")]
    chat_id = await make_chat(maker, env["instance_pk"])
    msg_id = await add_queued(maker, chat_id, "", media_type=MediaType.DOCUMENT)

    assert await env["pipeline"].run_once() == 1

    message = await get_message(maker, msg_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert await get_reply(maker, chat_id) is not None


# --------------------------- склейка батча ------------------------------


def test_identical_repeats_collapse_into_one_line() -> None:
    messages = [Message(body="Алло", state=MsgState.QUEUED) for _ in range(5)]
    assert merged_parts(messages) == ["Алло"]


def test_a_long_batch_keeps_the_most_recent_messages() -> None:
    """Six paragraphs typed over a minute are each fine alone and too
    long merged. Trimming beats refusing: the customer did split it."""
    messages = [Message(body="а" * 700, state=MsgState.QUEUED) for _ in range(5)]
    messages.append(Message(body="и сколько это стоит?", state=MsgState.QUEUED))
    parts = merged_parts(messages)

    assert len("\n".join(parts)) <= MAX_QUESTION_CHARS
    assert parts[-1] == "и сколько это стоит?"  # последнее сообщение сохранено
    assert len(parts) < len(messages)


async def test_five_identical_messages_in_one_batch_are_answered_not_ignored(env) -> None:
    """One batch is one attempt. «Алло» five times at a bot that is
    deliberately pausing used to trip the spam gate and produce silence
    — with no flag for a manager either."""
    maker = env["maker"]
    await set_debounce(maker, 0)
    chat_id = await make_chat(maker, env["instance_pk"])
    ids = [await add_queued(maker, chat_id, QUESTION) for _ in range(5)]

    await env["pipeline"].run_once()

    assert len(env["chat_provider"].requests) == 1
    asked = env["chat_provider"].requests[0].messages[-1]["content"]
    assert asked == QUESTION  # схлопнуто, а не пять строк подряд
    for message_id in ids:
        assert (await get_message(maker, message_id)).state is MsgState.ANSWERED


async def test_repeating_after_an_answer_is_still_spam_and_visible(env) -> None:
    """Repeats ACROSS attempts remain spam — and the chat is flagged, so
    the silence is never invisible."""
    maker = env["maker"]
    await set_debounce(maker, 0)
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.SPAM_REPEAT_THRESHOLD, 2)
    chat_id = await make_chat(maker, env["instance_pk"])
    await add_queued(maker, chat_id, "Алло")
    await env["pipeline"].run_once()  # первая попытка: отвечено

    second = await add_queued(maker, chat_id, "Алло")
    await env["pipeline"].run_once()  # вторая попытка тем же текстом

    message = await get_message(maker, second)
    assert message.state is MsgState.IGNORED
    assert message.error_code == "spam"
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
    assert chat.needs_attention
    assert chat.attention_reason is AttentionReason.HANDOFF


# ------------------------- склейка и бесплатный FAQ ----------------------


@pytest.fixture
async def faq_pair(db_engine) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        for key, answer, patterns in (
            ("address", ADDRESS_ANSWER, ["какой у вас адрес"]),
            ("working-hours", HOURS_ANSWER, ["во сколько вы работаете"]),
        ):
            faq = KbFaq(
                normalized_key=key,
                answer_ru=answer,
                answer_ru_status=AnswerStatus.APPROVED,
                is_active=True,
            )
            db.add(faq)
            await db.flush()
            for pattern in patterns:
                db.add(build_pattern(faq.id, KbLanguage.RU, pattern))


async def test_two_faq_questions_in_one_batch_stay_free(env, faq_pair) -> None:
    """Merged, their word set matches no pattern, and the pair used to
    buy a model call. Every part matching means every part is free."""
    maker = env["maker"]
    await set_debounce(maker, 0)
    chat_id = await make_chat(maker, env["instance_pk"])
    await add_queued(maker, chat_id, "какой у вас адрес")
    await add_queued(maker, chat_id, "во сколько вы работаете")

    await env["pipeline"].run_once()

    assert env["chat_provider"].requests == []  # модель не вызывалась
    reply = await get_reply(maker, chat_id)
    assert reply is not None
    assert ADDRESS_ANSWER in reply.body
    assert HOURS_ANSWER in reply.body


async def test_a_batch_with_one_unknown_question_goes_to_the_model(env, faq_pair) -> None:
    """All-or-nothing: answering only the FAQ half would leave the other
    question silently unanswered."""
    maker = env["maker"]
    await set_debounce(maker, 0)
    chat_id = await make_chat(maker, env["instance_pk"])
    await add_queued(maker, chat_id, "какой у вас адрес")
    await add_queued(maker, chat_id, QUESTION)

    await env["pipeline"].run_once()

    assert len(env["chat_provider"].requests) == 1
    reply = await get_reply(maker, chat_id)
    assert reply is not None and reply.body == ANSWER


# ------------------------------ мониторинг -------------------------------


async def test_the_queue_metric_ignores_messages_still_inside_the_window(env) -> None:
    """With a minute-long pause everything written in the last minute is
    waiting BY CONFIGURATION. Counting it raised «очередь растёт» on an
    ordinary evening spike."""
    maker = env["maker"]
    await set_debounce(maker, 60)
    chat_id = await make_chat(maker, env["instance_pk"])
    waiting = await add_queued(maker, chat_id, "только что")
    overdue = await add_queued(maker, chat_id, "давно")
    await age_message(maker, overdue, seconds=300)

    async with maker() as db:
        snapshot = await monitoring.collect(db, media_dir=str(env["deps"].settings.media_dir))

    assert snapshot.queue_depth == 1  # просроченное, а не оба
    assert waiting  # сообщение существует, просто ещё ждёт склейку


# ------------------------------ настройка --------------------------------


async def test_the_owner_can_save_sixty_seconds(authed_client) -> None:
    client, headers = authed_client
    current = (await client.get("/api/limits")).json()["settings"]
    response = await client.put(
        "/api/limits", json={"settings": {**current, "debounce_seconds": 60}}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["settings"]["debounce_seconds"] == 60


async def test_a_pause_that_eats_the_stale_window_is_refused(authed_client) -> None:
    """Both numbers are valid alone; together they mean the bot waits out
    a fifth of the window after which it refuses to answer at all."""
    client, headers = authed_client
    current = (await client.get("/api/limits")).json()["settings"]
    response = await client.put(
        "/api/limits",
        json={"settings": {**current, "debounce_seconds": 60, "ai_max_message_age_minutes": 5}},
        headers=headers,
    )
    assert response.status_code == 422
    assert "Задержка" in response.text


async def test_analytics_still_sees_the_answer_as_one(env, faq_pair) -> None:
    """A merged batch is several incoming messages and one answer; the
    automation percentage must not read that as half-failure."""
    maker = env["maker"]
    await set_debounce(maker, 0)
    chat_id = await make_chat(maker, env["instance_pk"])
    await add_queued(maker, chat_id, "какой у вас адрес")
    await add_queued(maker, chat_id, "во сколько вы работаете")
    await env["pipeline"].run_once()

    async with maker() as db:
        work = await analytics.workload(db, analytics.period_from_days(1))
    assert work["incoming"] == 2
    assert work["answered_by_ai"] == 2
    assert work["automation_percent"] == 100
