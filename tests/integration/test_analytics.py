"""Reports must agree with the rows they claim to summarise.

The DoD of this phase is not "a chart appears" — it is that every number
can be re-derived from ai_calls/messages with a second query, that a
free answer is counted at all, that opening a report costs nothing, and
that the queries use indexes instead of scanning the tables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.models import AiCall, BudgetLedger, Chat, Message, WhatsappInstance
from app.models.enums import (
    AnswerSource,
    BudgetScope,
    CallType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services import analytics
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

TODAY = datetime.now(UTC)


@pytest.fixture
async def seeded(db_engine):
    """A small but realistic period: paid answers, free answers, a
    handoff, an ignored message and a barrier row that must NOT count."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        instance = WhatsappInstance(
            provider="green_api", instance_id="1101000001", token_ref="GREEN_API_TOKEN"
        )
        db.add(instance)
        await db.flush()
        chats = []
        for i in range(2):
            chat = Chat(
                instance_id=instance.id,
                wa_chat_id=f"7701000000{i}@c.us",
                phone=f"+7701000000{i}",
            )
            db.add(chat)
            chats.append(chat)
        await db.flush()

        def call(chat, *, call_type, usd, kzt, days_ago=0, status="ok", **tokens):
            return AiCall(
                correlation_id=f"c-{chat.id}-{call_type.value}-{days_ago}-{status}",
                chat_id=chat.id,
                call_type=call_type,
                model="gpt-5.6-luna" if call_type is not CallType.EMBEDDING else "emb",
                cost_usd=Decimal(usd),
                cost_kzt=Decimal(kzt),
                status=status,
                created_at=TODAY - timedelta(days=days_ago),
                **tokens,
            )

        db.add_all(
            [
                call(
                    chats[0],
                    call_type=CallType.CHAT,
                    usd="0.00030000",
                    kzt="0.1500",
                    input_tokens=1000,
                    cached_input_tokens=200,
                    output_tokens=100,
                    reasoning_tokens=50,
                ),
                call(
                    chats[0],
                    call_type=CallType.EMBEDDING,
                    usd="0.00001000",
                    kzt="0.0050",
                    input_tokens=500,
                ),
                call(
                    chats[1],
                    call_type=CallType.CHAT,
                    usd="0.00090000",
                    kzt="0.4500",
                    days_ago=1,
                    input_tokens=3000,
                    output_tokens=200,
                ),
                # The media double-payment barrier: a placeholder, not spend.
                call(
                    chats[1],
                    call_type=CallType.TRANSCRIPTION,
                    usd="0",
                    kzt="0",
                    status="started",
                ),
            ]
        )

        def message(chat, **kwargs):
            base = {
                "chat_id": chat.id,
                "direction": MsgDirection.IN,
                "source": MsgSource.CUSTOMER,
                "state": MsgState.ANSWERED,
                "created_at": TODAY,
            }
            base.update(kwargs)
            return Message(**base)

        db.add_all(
            [
                message(chats[0], provider_message_id="in-1"),
                message(chats[0], provider_message_id="in-2"),
                message(
                    chats[1],
                    provider_message_id="in-3",
                    state=MsgState.HANDED_TO_HUMAN,
                    error_code="media_unsupported",
                ),
                message(
                    chats[1],
                    provider_message_id="in-4",
                    state=MsgState.IGNORED,
                    error_code="stale",
                ),
                # Outgoing answers, each tagged with how it was produced.
                message(
                    chats[0],
                    provider_message_id=None,
                    direction=MsgDirection.OUT,
                    source=MsgSource.AI,
                    state=MsgState.SENT,
                    answer_source=AnswerSource.MODEL,
                ),
                message(
                    chats[0],
                    provider_message_id=None,
                    direction=MsgDirection.OUT,
                    source=MsgSource.AI,
                    state=MsgState.SENT,
                    answer_source=AnswerSource.FAQ,
                ),
                message(
                    chats[1],
                    provider_message_id=None,
                    direction=MsgDirection.OUT,
                    source=MsgSource.OPERATOR_PHONE,
                    state=MsgState.SENT,
                ),
            ]
        )
    return maker


# --------------------------- numbers agree ------------------------------


async def test_daily_spend_matches_a_second_query(seeded) -> None:
    """Every chart value must be re-derivable from ai_calls."""
    period = analytics.period_from_days(7)
    async with seeded() as db:
        rows = await analytics.spend_by_day(db, period)
        truth = (
            await db.execute(
                select(func.coalesce(func.sum(AiCall.cost_usd), 0)).where(
                    AiCall.created_at >= period.start_at,
                    AiCall.created_at < period.end_at,
                    AiCall.status != "started",
                )
            )
        ).scalar_one()
    assert sum((row.total_usd for row in rows), Decimal(0)) == truth
    assert len(rows) == 7  # empty days are present, not skipped


async def test_barrier_rows_are_not_counted_as_spend(seeded) -> None:
    """`status='started'` exists only to stop a second charge for one
    media file; counting it would invent money that never left."""
    period = analytics.period_from_days(7)
    async with seeded() as db:
        by_model = await analytics.spend_by_model(db, period)
    assert all(row["call_type"] != CallType.TRANSCRIPTION.value for row in by_model)


async def test_free_answers_are_counted(seeded) -> None:
    """The reason column answer_source exists: an FAQ answer writes no
    ai_calls row, so before it there was nothing to count."""
    period = analytics.period_from_days(7)
    async with seeded() as db:
        sources = await analytics.answer_sources(db, period)
    assert sources["model"] == 1
    assert sources["faq"] == 1  # free, and now visible
    assert sources["cache"] == 0


async def test_automation_percent_ignores_what_was_never_answerable(seeded) -> None:
    """A stale or spam message is not a failure of the bot; counting it
    against automation would understate the truth."""
    period = analytics.period_from_days(7)
    async with seeded() as db:
        work = await analytics.workload(db, period)
    assert work["incoming"] == 4
    assert work["answered_by_ai"] == 2
    assert work["handed_to_human"] == 1
    assert work["ignored"] == 1
    assert work["answerable"] == 3
    assert work["automation_percent"] == 67  # 2 of 3, not 2 of 4
    assert work["replies_from_phone"] == 1


async def test_top_dialogs_sum_to_the_calls(seeded) -> None:
    period = analytics.period_from_days(7)
    async with seeded() as db:
        top = await analytics.top_dialogs(db, period)
        truth = (
            await db.execute(
                select(func.coalesce(func.sum(AiCall.cost_usd), 0)).where(
                    AiCall.chat_id.is_not(None), AiCall.status != "started"
                )
            )
        ).scalar_one()
    assert sum(Decimal(row["cost_usd"]) for row in top) == truth
    assert Decimal(top[0]["cost_usd"]) >= Decimal(
        top[-1]["cost_usd"]
    )  # most expensive first


async def test_forecast_is_arithmetic_on_real_spend(seeded) -> None:
    async with seeded() as db, db.begin():
        db.add(
            BudgetLedger(
                scope=BudgetScope.GLOBAL_MONTH,
                scope_key=TODAY.strftime("%Y-%m"),
                limit_usd=Decimal("6.00"),
                spent_usd=Decimal("3.00"),
                reserved_usd=Decimal(0),
                period_start=TODAY.date().replace(day=1),
                period_end=(TODAY.date().replace(day=28) + timedelta(days=4)).replace(
                    day=1
                )
                - timedelta(days=1),
            )
        )
    async with seeded() as db:
        state = await analytics.budget_state(db, now=TODAY)
    assert state["month"] is not None
    # Half the limit spent: the forecast exists and stays inside the month.
    assert state["forecast_date"] is not None
    assert state["forecast_date"] <= state["month"]["period_end"]


# ------------------------------- the DoD --------------------------------


async def test_reports_use_indexes_not_a_full_scan(seeded) -> None:
    """Phase DoD: the dashboard must not scan the tables. Checked with a
    real EXPLAIN against PostgreSQL, not by reading the query."""
    period = analytics.period_from_days(30)
    async with seeded() as db:
        await db.execute(
            text("SET enable_seqscan = off")
        )  # prove an index CAN serve it
        plan = (
            (
                await db.execute(
                    text(
                        "EXPLAIN (FORMAT TEXT) SELECT date(created_at), call_type, sum(cost_usd) "
                        "FROM ai_calls WHERE created_at >= :start AND created_at < :end "
                        "GROUP BY 1, 2"
                    ),
                    {"start": period.start_at, "end": period.end_at},
                )
            )
            .scalars()
            .all()
        )
    assert any("Index" in line for line in plan), plan


async def test_opening_a_report_never_calls_openai(authed_client, monkeypatch) -> None:
    """SPEC invariant 16: reading history and reports is free."""
    client, _ = authed_client

    def explode(*_args, **_kwargs):
        raise AssertionError("a report must never call OpenAI")

    monkeypatch.setattr("app.ai.chat_provider.OpenAIChatProvider.complete", explode)
    monkeypatch.setattr("app.ai.embeddings.OpenAIEmbeddingProvider.embed", explode)

    for path in (
        "/api/analytics/dashboard",
        "/api/analytics",
        "/api/analytics/costs?days=7",
    ):
        response = await client.get(path)
        assert response.status_code == 200, response.text


async def test_export_opens_in_excel(authed_client) -> None:
    """An .xlsx that Excel refuses is not an export."""
    from openpyxl import load_workbook

    client, _ = authed_client
    response = await client.get("/api/analytics/costs/export?days=7&fmt=xlsx")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    book = load_workbook(filename=__import__("io").BytesIO(response.content))
    assert book.active.title == "Расходы"
    assert book.active.cell(row=1, column=1).value == "Дата"


async def test_csv_export_survives_russian_excel(authed_client) -> None:
    """Without a BOM and semicolons, Excel in a Russian locale puts the
    whole row into one cell."""
    client, _ = authed_client
    response = await client.get("/api/analytics/costs/export?days=7&fmt=csv")

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM
    assert b";" in response.content


async def test_period_is_capped(authed_client) -> None:
    client, _ = authed_client
    assert (await client.get("/api/analytics/costs?days=99999")).status_code == 422
