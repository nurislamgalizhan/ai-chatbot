"""Aggregates for the Обзор / Расходы / Аналитика sections (SPEC §12.1,
§12.5, §12.7).

Two rules shape this module.

1. Every number comes from a row that exists. Where SPEC asks for a
   metric the schema cannot support honestly, the function is absent and
   the gap is written down in the gate report — a plausible number the
   owner would act on is worse than a missing one.
2. Nothing here calls OpenAI. Opening a report is free (SPEC invariant
   11/16); the only cost of analytics is a few indexed queries.

Day boundaries are UTC, matching how budget periods are keyed, so the
dashboard and the limits page cannot disagree about "today".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, Numeric, and_, case, cast, func, select
from sqlalchemy.orm import aliased

from app.models import AiCall, BudgetLedger, Chat, KbFaq, KbFaqMiss, Message
from app.models.enums import (
    AnswerSource,
    BudgetScope,
    CallType,
    MsgDirection,
    MsgSource,
    MsgState,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# A report is a reading tool: cap every list so one query cannot pull a
# year of rows into the panel.
TOP_LIMIT = 10
MAX_DAYS = 366


@dataclass(frozen=True)
class Period:
    """Inclusive day range in UTC."""

    start: date
    end: date

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start, datetime.min.time(), tzinfo=UTC)

    @property
    def end_at(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def each_day(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]


def period_from_days(days: int, *, today: date | None = None) -> Period:
    span = max(1, min(days, MAX_DAYS))
    end = today or datetime.now(UTC).date()
    return Period(start=end - timedelta(days=span - 1), end=end)


@dataclass
class DailySpend:
    day: date
    #: Per call type, in tenge — what the chart draws. Kept in the same
    #: currency as the totals so nothing has to be converted in the UI
    #: (a per-day USD→KZT ratio breaks on days with no spend).
    by_call_type: dict[str, Decimal] = field(default_factory=dict)
    total_usd: Decimal = Decimal(0)
    total_kzt: Decimal = Decimal(0)


@dataclass
class DailyTokens:
    day: date
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    audio_seconds: int = 0
    cost_usd: Decimal = Decimal(0)
    cost_kzt: Decimal = Decimal(0)


def _only_real_calls() -> ColumnElement[bool]:
    """Rows that represent a call that actually happened.

    `status='started'` rows exist only as the double-payment barrier for
    media (phase 7): counting them would invent spend that never left.
    """
    return AiCall.status != "started"


#: Series key for spend the OWNER caused himself from the panel — the
#: sandbox, the KB translation buttons, indexing the knowledge base. It
#: has no chat, so mixing it into «Ответы» would inflate what answering
#: customers actually costs.
ADMIN_SERIES = "admin"


async def spend_by_day(db: AsyncSession, period: Period) -> list[DailySpend]:
    """Daily spend split by call type — the main chart of «Расходы»."""
    day = func.date(AiCall.created_at).label("day")
    admin = AiCall.chat_id.is_(None).label("admin")
    rows = (
        await db.execute(
            select(
                day,
                AiCall.call_type,
                admin,
                func.sum(AiCall.cost_usd).label("usd"),
                func.sum(AiCall.cost_kzt).label("kzt"),
            )
            .where(
                AiCall.created_at >= period.start_at,
                AiCall.created_at < period.end_at,
                _only_real_calls(),
            )
            .group_by(day, AiCall.call_type, admin)
        )
    ).all()

    buckets = {d: DailySpend(day=d) for d in period.each_day()}
    for row_day, call_type, is_admin, usd, kzt in rows:
        bucket = buckets.get(row_day)
        if bucket is None:
            continue
        key = ADMIN_SERIES if is_admin else call_type.value
        bucket.by_call_type[key] = bucket.by_call_type.get(key, Decimal(0)) + Decimal(kzt or 0)
        bucket.total_usd += Decimal(usd or 0)
        bucket.total_kzt += Decimal(kzt or 0)
    return [buckets[d] for d in period.each_day()]


async def tokens_by_day(db: AsyncSession, period: Period) -> list[DailyTokens]:
    """The table under the chart: what those tokens were."""
    day = func.date(AiCall.created_at).label("day")
    rows = (
        await db.execute(
            select(
                day,
                func.sum(AiCall.input_tokens),
                func.sum(AiCall.cached_input_tokens),
                func.sum(AiCall.output_tokens),
                func.sum(AiCall.reasoning_tokens),
                func.sum(AiCall.audio_seconds),
                func.sum(AiCall.cost_usd),
                func.sum(AiCall.cost_kzt),
            )
            .where(
                AiCall.created_at >= period.start_at,
                AiCall.created_at < period.end_at,
                _only_real_calls(),
            )
            .group_by(day)
            .order_by(day)
        )
    ).all()
    return [
        DailyTokens(
            day=row[0],
            input_tokens=int(row[1] or 0),
            cached_input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            reasoning_tokens=int(row[4] or 0),
            audio_seconds=int(row[5] or 0),
            cost_usd=Decimal(row[6] or 0),
            cost_kzt=Decimal(row[7] or 0),
        )
        for row in rows
    ]


async def spend_by_model(db: AsyncSession, period: Period) -> list[dict[str, object]]:
    admin = AiCall.chat_id.is_(None).label("admin")
    rows = (
        await db.execute(
            select(
                AiCall.model,
                AiCall.call_type,
                admin,
                func.count().label("calls"),
                func.sum(AiCall.cost_usd),
                func.sum(AiCall.cost_kzt),
            )
            .where(
                AiCall.created_at >= period.start_at,
                AiCall.created_at < period.end_at,
                _only_real_calls(),
            )
            .group_by(AiCall.model, AiCall.call_type, admin)
            .order_by(func.sum(AiCall.cost_usd).desc())
        )
    ).all()
    return [
        {
            "model": model,
            # Same split as the chart: the owner's own clicks are not
            # what answering a customer costs.
            "call_type": ADMIN_SERIES if is_admin else call_type.value,
            "calls": int(calls),
            "cost_usd": str(Decimal(usd or 0)),
            "cost_kzt": str(Decimal(kzt or 0)),
        }
        for model, call_type, is_admin, calls, usd, kzt in rows
    ]


async def top_dialogs(db: AsyncSession, period: Period) -> list[dict[str, object]]:
    """Most expensive dialogs, summed from the calls themselves — the
    single source of truth for what a dialog cost."""
    rows = (
        await db.execute(
            select(
                AiCall.chat_id,
                Chat.phone,
                Chat.display_name,
                func.count().label("calls"),
                func.sum(AiCall.cost_usd),
                func.sum(AiCall.cost_kzt),
            )
            .join(Chat, Chat.id == AiCall.chat_id)
            .where(
                AiCall.created_at >= period.start_at,
                AiCall.created_at < period.end_at,
                AiCall.chat_id.is_not(None),
                _only_real_calls(),
            )
            .group_by(AiCall.chat_id, Chat.phone, Chat.display_name)
            .order_by(func.sum(AiCall.cost_usd).desc())
            .limit(TOP_LIMIT)
        )
    ).all()
    return [
        {
            "chat_id": chat_id,
            "phone": phone,
            "display_name": display_name,
            "calls": int(calls),
            "cost_usd": str(Decimal(usd or 0)),
            "cost_kzt": str(Decimal(kzt or 0)),
        }
        for chat_id, phone, display_name, calls, usd, kzt in rows
    ]


async def answer_sources(db: AsyncSession, period: Period) -> dict[str, int]:
    """How many answers came from the model, FAQ, cache or a service
    message. Only possible because the pipeline records it (0008): a free
    answer writes no ai_calls row and would otherwise be invisible."""
    rows = (
        await db.execute(
            select(Message.answer_source, func.count())
            .where(
                Message.created_at >= period.start_at,
                Message.created_at < period.end_at,
                Message.answer_source.is_not(None),
            )
            .group_by(Message.answer_source)
        )
    ).all()
    counts = {source.value: 0 for source in AnswerSource}
    for source, count in rows:
        counts[source.value] = int(count)
    return counts


async def workload(db: AsyncSession, period: Period) -> dict[str, int]:
    """Who handled the conversation: the bot, or a person.

    The automation share deliberately counts only messages that COULD
    have been answered — ignored ones (stale, spam, stickers) are not
    failures of the bot and would drag the number down dishonestly.
    """
    reply = aliased(Message)
    rows = (
        await db.execute(
            select(
                Message.direction,
                Message.source,
                Message.state,
                reply.answer_source,
                func.count(),
            )
            .select_from(Message)
            # The reply to a customer message shares its correlation id;
            # its answer_source says whether the bot answered (model, FAQ,
            # cache) or only phrased a handoff (service).
            .outerjoin(
                reply,
                and_(
                    Message.direction == MsgDirection.IN,
                    reply.correlation_id == Message.correlation_id,
                    reply.direction == MsgDirection.OUT,
                    reply.source == MsgSource.AI,
                ),
            )
            .where(
                Message.created_at >= period.start_at,
                Message.created_at < period.end_at,
            )
            .group_by(Message.direction, Message.source, Message.state, reply.answer_source)
        )
    ).all()

    incoming = 0
    answered_by_ai = 0
    handed_to_human = 0
    ignored = 0
    by_panel = 0
    by_phone = 0
    for direction, source, state, answer_source, raw_count in rows:
        amount = int(raw_count)
        if direction is MsgDirection.IN and source is MsgSource.CUSTOMER:
            incoming += amount
            if state is MsgState.ANSWERED and answer_source is AnswerSource.SERVICE:
                # «Передал менеджеру» composed by the model is not an
                # answer — the customer still waits for a human.
                handed_to_human += amount
            elif state is MsgState.ANSWERED:
                answered_by_ai += amount
            elif state in (MsgState.HANDED_TO_HUMAN, MsgState.FAILED_FINAL):
                # A processing failure is the bot's failure to automate,
                # not "never answerable": it counts against automation.
                handed_to_human += amount
            elif state in (MsgState.IGNORED, MsgState.BLOCKED_BY_LIMIT):
                ignored += amount
        elif direction is MsgDirection.OUT:
            if source is MsgSource.OPERATOR_PANEL:
                by_panel += amount
            elif source is MsgSource.OPERATOR_PHONE:
                by_phone += amount
    answerable = answered_by_ai + handed_to_human
    return {
        "incoming": incoming,
        "answered_by_ai": answered_by_ai,
        "handed_to_human": handed_to_human,
        "ignored": ignored,
        "replies_from_panel": by_panel,
        "replies_from_phone": by_phone,
        "answerable": answerable,
        "automation_percent": round(answered_by_ai * 100 / answerable) if answerable else 0,
    }


async def dialog_stats(db: AsyncSession, period: Period) -> dict[str, object]:
    total = (await db.execute(select(func.count()).select_from(Chat))).scalar_one()
    fresh = (
        await db.execute(
            select(func.count())
            .select_from(Chat)
            .where(Chat.created_at >= period.start_at, Chat.created_at < period.end_at)
        )
    ).scalar_one()
    spend, chats_with_spend = (
        await db.execute(
            select(
                func.coalesce(func.sum(AiCall.cost_kzt), 0),
                func.count(func.distinct(AiCall.chat_id)),
            ).where(
                AiCall.created_at >= period.start_at,
                AiCall.created_at < period.end_at,
                AiCall.chat_id.is_not(None),
                _only_real_calls(),
            )
        )
    ).one()
    average = Decimal(spend or 0) / chats_with_spend if chats_with_spend else Decimal(0)
    return {
        "total": int(total),
        "new": int(fresh),
        "with_spend": int(chats_with_spend),
        "average_cost_kzt": str(average.quantize(Decimal("0.0001"))),
    }


async def errors(db: AsyncSession, period: Period) -> list[dict[str, object]]:
    """Failures worth a human's attention, grouped by their code."""
    rows = (
        await db.execute(
            select(Message.error_code, func.count())
            .where(
                Message.created_at >= period.start_at,
                Message.created_at < period.end_at,
                Message.error_code.is_not(None),
            )
            .group_by(Message.error_code)
            .order_by(func.count().desc())
            .limit(TOP_LIMIT)
        )
    ).all()
    return [{"code": code, "count": int(count)} for code, count in rows]


async def language_gaps(db: AsyncSession, period: Period) -> list[dict[str, object]]:
    """What to translate next: questions asked in a language with no
    approved answer (owner's language policy)."""
    rows = (
        await db.execute(
            select(
                KbFaqMiss.language,
                KbFaqMiss.faq_id,
                KbFaq.normalized_key,
                func.count().label("misses"),
            )
            .join(KbFaq, KbFaq.id == KbFaqMiss.faq_id, isouter=True)
            .where(
                KbFaqMiss.created_at >= period.start_at,
                KbFaqMiss.created_at < period.end_at,
            )
            .group_by(KbFaqMiss.language, KbFaqMiss.faq_id, KbFaq.normalized_key)
            .order_by(func.count().desc())
            .limit(TOP_LIMIT)
        )
    ).all()
    return [
        {
            "language": language.value,
            "faq_id": faq_id,
            "key": key,
            "misses": int(misses),
        }
        for language, faq_id, key, misses in rows
    ]


async def _calls_spend_usd(db: AsyncSession, since: date, until: datetime) -> Decimal:
    """Everything ai_calls says was actually paid in the window."""
    total = (
        await db.execute(
            select(func.sum(AiCall.cost_usd)).where(
                AiCall.created_at >= datetime.combine(since, time.min, tzinfo=UTC),
                AiCall.created_at <= until,
                _only_real_calls(),
            )
        )
    ).scalar()
    return Decimal(total or 0)


async def budget_state(db: AsyncSession, *, now: datetime | None = None) -> dict[str, object]:
    """Today's and this month's budget, plus an honest projection.

    The projection is arithmetic on real spend, and it says so: at the
    current daily rate, the month's limit is used up on this date.
    """
    moment = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(BudgetLedger).where(
                    BudgetLedger.scope.in_((BudgetScope.GLOBAL_DAY, BudgetScope.GLOBAL_MONTH)),
                    BudgetLedger.period_start <= moment.date(),
                    BudgetLedger.period_end >= moment.date(),
                )
            )
        )
        .scalars()
        .all()
    )

    state: dict[str, object] = {
        "day": None,
        "month": None,
        "forecast_date": None,
        "month_calls_usd": "0",
    }
    month_row = None
    for row in rows:
        payload = {
            "limit_usd": str(row.limit_usd),
            "spent_usd": str(row.spent_usd),
            "reserved_usd": str(row.reserved_usd),
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
        }
        if row.scope is BudgetScope.GLOBAL_DAY:
            state["day"] = payload
        else:
            state["month"] = payload
            month_row = row

    if month_row is not None:
        # The WALL is the ledger: that is the number the reservation SQL
        # compares against, so that is what will actually stop the bot.
        # The RATE is ai_calls: the ledger misses spend that is recorded
        # but never reserved (embeddings), so a forecast built on it
        # would promise more days than the money buys.
        real_month_usd = await _calls_spend_usd(db, month_row.period_start, moment)
        state["month_calls_usd"] = str(real_month_usd)
        elapsed = (moment.date() - month_row.period_start).days + 1
        per_day = real_month_usd / elapsed
        left = Decimal(month_row.limit_usd) - Decimal(month_row.spent_usd)
        if left <= 0:
            state["forecast_date"] = moment.date().isoformat()
        elif per_day > 0:
            days_left = int(left / per_day)
            projected = moment.date() + timedelta(days=days_left)
            # Never promise beyond the period it describes.
            state["forecast_date"] = min(projected, month_row.period_end).isoformat()
    return state


async def dashboard(db: AsyncSession, *, now: datetime | None = None) -> dict[str, object]:
    """Everything the Обзор page shows, in one round of queries."""
    moment = now or datetime.now(UTC)
    today = Period(start=moment.date(), end=moment.date())
    month_start = moment.date().replace(day=1)
    month = Period(start=month_start, end=moment.date())

    queue_depth = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.state == MsgState.QUEUED,
                Message.direction == MsgDirection.IN,
                Message.source == MsgSource.CUSTOMER,
            )
        )
    ).scalar_one()
    attention = (
        await db.execute(
            select(func.count()).select_from(Chat).where(Chat.needs_attention.is_(True))
        )
    ).scalar_one()
    unread = (
        await db.execute(select(func.count()).select_from(Chat).where(Chat.unread_count > 0))
    ).scalar_one()
    failures_24h = (
        await db.execute(
            select(func.count())
            .select_from(AiCall)
            .where(AiCall.created_at >= moment - timedelta(hours=24), AiCall.status == "error")
        )
    ).scalar_one()

    spend_today, spend_month = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (AiCall.created_at >= today.start_at, AiCall.cost_kzt),
                            else_=cast(0, Numeric(12, 4)),
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(AiCall.cost_kzt), 0),
            ).where(
                AiCall.created_at >= month.start_at,
                AiCall.created_at < month.end_at,
                _only_real_calls(),
            )
        )
    ).one()

    return {
        "queue_depth": int(queue_depth),
        "needs_attention": int(attention),
        "unread_chats": int(unread),
        "failures_24h": int(failures_24h),
        "spend_today_kzt": str(Decimal(spend_today or 0)),
        "spend_month_kzt": str(Decimal(spend_month or 0)),
        "today": await workload(db, today),
        "budget": await budget_state(db, now=moment),
    }


def call_type_labels() -> dict[str, str]:
    """Owner-facing names; the enum values stay in the database."""
    return {
        CallType.CHAT.value: "Ответы",
        CallType.TRANSCRIPTION.value: "Распознавание речи",
        CallType.EMBEDDING.value: "Поиск по базе знаний",
        CallType.VISION.value: "Распознавание фото",
        ADMIN_SERIES: "Моя работа в панели",
    }
