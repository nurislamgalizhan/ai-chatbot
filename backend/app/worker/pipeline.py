"""The AI pipeline: consumes QUEUED customer messages (SPEC §6).

Discipline that keeps money and messages safe:
- correlation_id is DETERMINISTIC ("chat:{id}:msg:{last_id}") and the
  reply's outbound_message_id is uuid5 of it, so any retry finds the
  already-GENERATED reply instead of paying for a second generation;
- the reply text is persisted as GENERATED BEFORE it is enqueued for
  sending (invariant 3): a send retry retries only the send;
- a PROCESSING claim carries a lease (next_attempt_at): the sweeper
  recovers work orphaned by a crash — finishing it if the reply exists,
  re-queueing it otherwise;
- gates run before anything is claimed; every gated exit costs nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ai.context_builder import HistoryTurn
from app.models import AiCall, Chat, ChatMemory, Message, SystemEvent
from app.models.enums import (
    AnswerSource,
    AttentionReason,
    CallType,
    KbLanguage,
    LastActor,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.security import rate_limit
from app.services import app_settings
from app.services.agent import get_active_instruction
from app.services.alerts import hour_bucket, record_alert
from app.services.app_settings import get_setting
from app.services.chat_memory import remember, snapshot
from app.services.chat_modes import ai_allowed
from app.services.chats import set_attention
from app.services.escalation import escalation_reason
from app.services.generation import (
    GenerationInput,
    compose_service_reply,
    generate_reply,
)
from app.services.kb_text import detect_language, normalize_query
from app.services.media_processing import PROCESSABLE, has_open_media_job
from app.whatsapp.outbox import enqueue_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.generation import GenerationDeps, GenerationOutcome

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Claim:
    chat_id: int
    instance_id: int
    wa_chat_id: str
    correlation_id: str
    message_ids: list[int]
    retry_count: int
    language: KbLanguage
    generation_input: GenerationInput
    #: Provider id of the customer message being answered, so the reply
    #: can quote it. None when the provider gave us no id.
    quote_provider_message_id: str | None = None


@dataclass(frozen=True)
class _MediaAck:
    """A file the bot cannot read: everything needed to say so, once the
    claim transaction is closed."""

    chat_id: int
    instance_id: int
    wa_chat_id: str
    phone: str
    correlation_id: str
    language: KbLanguage
    style: str | None
    #: Which service wording to use. A real file ("media") and a pin on
    #: the map or a contact card ("unsupported_content") are different
    #: things, and telling a customer "I passed your FILE to a manager"
    #: when they sent their location reads as a broken bot.
    reason: str = "media"


@dataclass(frozen=True)
class _ClaimResult:
    """What one claim produced: work to answer, a file to acknowledge,
    or neither."""

    claim: _Claim | None = None
    media_ack: _MediaAck | None = None

    @property
    def did_work(self) -> bool:
        return self.claim is not None or self.media_ack is not None


# Namespace for uuid5(correlation) -> deterministic outbound ids.
REPLY_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

_CHAT_BATCH = 20
_HISTORY_LIMIT = 10
# The PROCESSING lease: a claim older than this with no progress is an
# orphan (crashed worker) and belongs to the sweeper.
_PROCESSING_LEASE = timedelta(minutes=10)
_NOT_CONFIGURED_RETRY = timedelta(minutes=5)
_RETRY_BACKOFF_BASE_SECONDS = 30
_HISTORY_ASSISTANT_SOURCES = (MsgSource.AI, MsgSource.OPERATOR_PANEL, MsgSource.OPERATOR_PHONE)


def correlation_for(chat_id: int, last_message_id: int) -> str:
    """The deterministic id that ties a batch, its paid call, its budget
    reservation and its reply together."""
    return f"chat:{chat_id}:msg:{last_message_id}"


def reply_outbound_id(correlation_id: str) -> uuid.UUID:
    return uuid.uuid5(REPLY_NAMESPACE, correlation_id)


def answer_source_of(kind: str) -> AnswerSource:
    """Map a generation outcome to what the reports count.

    Everything that is not a model answer, an FAQ hit or a cache hit is
    a service message (a refusal, a handoff, a file acknowledgement).
    """
    if kind == "model":
        return AnswerSource.MODEL
    if kind == "faq":
        return AnswerSource.FAQ
    if kind == "cache":
        return AnswerSource.CACHE
    return AnswerSource.SERVICE


def message_text(message: Message) -> str:
    """What the model sees for one message.

    A voice note's words and an image's description live in `transcript`
    (phase 7), the typed text and any file caption in `body`. Both are
    included, caption first, so a photo sent with "сколько стоит?" keeps
    both halves of the question — and so the description is reused in
    later turns instead of the picture being sent again (SPEC §13).
    """
    parts = [(message.body or "").strip(), (message.transcript or "").strip()]
    return "\n".join(part for part in parts if part)


class AiPipeline:
    def __init__(self, deps: GenerationDeps) -> None:
        self._deps = deps

    # ------------------------------ pickup ------------------------------

    async def run_once(self) -> int:
        """One pass: find chats with due QUEUED messages, process them."""
        now = datetime.now(UTC)
        async with self._deps.sessionmaker() as db:
            debounce = int(await get_setting(db, app_settings.DEBOUNCE_SECONDS))
            rows = (
                await db.execute(
                    select(Message.chat_id, func.max(Message.created_at))
                    .where(
                        Message.state == MsgState.QUEUED,
                        Message.direction == MsgDirection.IN,
                        Message.source == MsgSource.CUSTOMER,
                        or_(
                            Message.next_attempt_at.is_(None),
                            Message.next_attempt_at <= now,
                        ),
                        # A chat still waiting on voice/image pre-processing
                        # is NOT due: answering its text now would reply out
                        # of order, and re-picking a chat we cannot finish
                        # would spin this loop at 100% CPU (it only sleeps
                        # when a pass handles nothing).
                        ~has_open_media_job(Message.chat_id),
                    )
                    .group_by(Message.chat_id)
                    .order_by(func.max(Message.created_at))
                    .limit(_CHAT_BATCH)
                )
            ).all()
        due = [chat_id for chat_id, last_at in rows if last_at <= now - timedelta(seconds=debounce)]
        if not due:
            return 0
        semaphore = asyncio.Semaphore(8)  # SPEC §9.3 MAX_CONCURRENT_AI_REQUESTS

        async def guarded(chat_id: int) -> int:
            async with semaphore:
                try:
                    return await self.process_chat(chat_id)
                except Exception:
                    logger.exception("pipeline_chat_failed", chat_id=chat_id)
                    return 0

        results = await asyncio.gather(*(guarded(chat_id) for chat_id in due))
        return sum(results)

    async def process_chat(self, chat_id: int) -> int:
        """Per-chat lock -> gates -> claim -> generate -> persist."""
        lock_key = f"lock:pipeline:chat:{chat_id}"
        token = uuid.uuid4().hex
        ttl = int(self._deps.settings.openai_request_timeout) + 120
        acquired = await self._deps.redis.set(lock_key, token, nx=True, ex=ttl)
        if not acquired:
            return 0
        try:
            result = await self._claim(chat_id)
            # The acknowledgement is composed AFTER the claim commits:
            # its model call must not run while the chat row is locked.
            if result.media_ack is not None:
                await self._send_media_ack(result.media_ack)
            if result.claim is None:
                # Nothing claimed and nothing said. Reporting work here
                # would keep the worker loop from ever sleeping (it idles
                # only on a zero pass) and burn a core on a 2-vCPU VPS.
                return 1 if result.did_work else 0
            if await self._already_charged(result.claim):
                # We paid for this exact question on an earlier attempt
                # and lost the answer before it was stored (a crash
                # between the settled call and the reply row). Paying a
                # second time for the same message is the one thing the
                # money rules forbid, so the dialog goes to a human.
                await self._hand_over_charged(result.claim)
                return 1
            outcome = await generate_reply(self._deps, result.claim.generation_input)
            await self._persist(result.claim, outcome)
            return 1
        finally:
            # Token-guarded release (best effort; the TTL is the backstop).
            current = await self._deps.redis.get(lock_key)
            holder = current.decode() if isinstance(current, bytes) else current
            if holder == token:
                await self._deps.redis.delete(lock_key)

    async def _already_charged(self, claim: _Claim) -> bool:
        """True if these messages were already paid for and the answer
        was lost before it reached the database.

        The check covers EVERY message in the batch, not just the last
        one: a customer who writes again while the crash is being
        recovered shifts the correlation id (it is built from the last
        message), and a barrier that only looked at the new id would let
        the same question be paid for twice.
        """
        candidates = [
            correlation_for(claim.chat_id, message_id) for message_id in claim.message_ids
        ]
        async with self._deps.sessionmaker() as db:
            paid = (
                (
                    await db.execute(
                        select(AiCall.correlation_id).where(
                            AiCall.correlation_id.in_(candidates),
                            AiCall.call_type == CallType.CHAT,
                            AiCall.status == "ok",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not paid:
                return False
            answered = (
                (
                    await db.execute(
                        select(Message.outbound_message_id).where(
                            Message.outbound_message_id.in_([reply_outbound_id(c) for c in paid])
                        )
                    )
                )
                .scalars()
                .all()
            )
            delivered = set(answered)
            return any(reply_outbound_id(c) not in delivered for c in paid)

    async def _hand_over_charged(self, claim: _Claim) -> None:
        async with self._deps.sessionmaker() as db:
            chat = await db.get(Chat, claim.chat_id, with_for_update=True)
            messages = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.id.in_(claim.message_ids),
                            Message.state == MsgState.PROCESSING,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for message in messages:
                message.state = MsgState.HANDED_TO_HUMAN
                message.error_code = "already_charged"
                message.next_attempt_at = None
            if chat is not None:
                await set_attention(
                    db, chat, AttentionReason.PROCESSING_ERROR, claim.correlation_id
                )
            db.add(
                SystemEvent(
                    level="error",
                    component="ai_pipeline",
                    event="already_charged",
                    detail={"correlation_id": claim.correlation_id},
                    correlation_id=claim.correlation_id,
                )
            )
            await db.commit()
        logger.error("pipeline_already_charged", correlation_id=claim.correlation_id)

    # ------------------------------ claim -------------------------------

    async def _claim(self, chat_id: int) -> _ClaimResult:  # noqa: PLR0911, PLR0912, PLR0915 - the gate ladder IS the SPEC §6.7 order
        now = datetime.now(UTC)
        async with self._deps.sessionmaker() as db:
            chat = await db.get(Chat, chat_id, with_for_update=True)
            if chat is None:
                return _ClaimResult()
            messages = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.state == MsgState.QUEUED,
                            Message.direction == MsgDirection.IN,
                            Message.source == MsgSource.CUSTOMER,
                            or_(
                                Message.next_attempt_at.is_(None),
                                Message.next_attempt_at <= now,
                            ),
                        )
                        .order_by(Message.id)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not messages:
                await db.commit()
                return _ClaimResult()

            max_age_minutes = int(await get_setting(db, app_settings.AI_MAX_MESSAGE_AGE_MINUTES))
            emergency_off = bool(await get_setting(db, app_settings.EMERGENCY_AI_OFF))
            per_minute = int(await get_setting(db, app_settings.MAX_MESSAGES_PER_USER_PER_MINUTE))
            per_hour = int(await get_setting(db, app_settings.MAX_MESSAGES_PER_USER_PER_HOUR))
            spam_threshold = int(await get_setting(db, app_settings.SPAM_REPEAT_THRESHOLD))

            # 1. Stale cutoff (owner decision 2026-09-06: 1 hour): the bot
            # answering yesterday's message does more harm than silence.
            # Measured from the CLIENT's send time (sent_at from the
            # WhatsApp payload) — after a worker outage old notifications
            # are ingested "now" and created_at would make them look fresh.
            cutoff = now - timedelta(minutes=max_age_minutes)
            fresh: list[Message] = []
            stale_found = False
            for message in messages:
                if (message.sent_at or message.created_at) < cutoff:
                    message.state = MsgState.IGNORED
                    message.error_code = "stale"
                    stale_found = True
                else:
                    fresh.append(message)
            if stale_found:
                await set_attention(db, chat, AttentionReason.HANDOFF)

            # 2. Media. Voice and images arrive here already turned into
            # text by the media processor (phase 7) and are answered like
            # any other message. Everything else — documents, video, gif
            # — is never sent to OpenAI: a human takes over, and a free
            # templated reply (no model call, no budget reserve) tells
            # the customer their file arrived instead of leaving them in
            # silence, but only when the bot may speak in this chat at
            # all (same gate as step 3).
            text_messages: list[Message] = []
            media_messages: list[Message] = []
            for message in fresh:
                if message.media_type in PROCESSABLE and (message.transcript or "").strip():
                    text_messages.append(message)
                elif message.media_type is not None:
                    message.state = MsgState.HANDED_TO_HUMAN
                    # OTHER is not a file: it is content we describe in
                    # words (location, contact card, poll). Everything
                    # else here IS a file we do not read.
                    message.error_code = (
                        "unsupported_content"
                        if message.media_type is MediaType.OTHER
                        else "media_unsupported"
                    )
                    media_messages.append(message)
                elif not (message.body or "").strip():
                    message.state = MsgState.IGNORED
                    message.error_code = "empty"
                else:
                    text_messages.append(message)
            media_ack: _MediaAck | None = None
            if media_messages:
                await set_attention(db, chat, AttentionReason.HANDOFF)
                if ai_allowed(chat, emergency_off=emergency_off):
                    media_ack = await self._plan_media_ack(db, chat, media_messages)
            if not text_messages:
                await db.commit()
                return _ClaimResult(media_ack=media_ack)

            # 3. THE mode gate (недочёт №5): emergency switch + chat mode.
            if not ai_allowed(chat, emergency_off=emergency_off):
                for message in text_messages:
                    message.state = MsgState.HANDED_TO_HUMAN
                    message.error_code = "ai_off"
                await db.commit()
                return _ClaimResult(media_ack=media_ack)

            # 4. Client rate limit, then spam — free exits, no reply.
            phone_ok_min = await rate_limit.allow(
                self._deps.redis, f"rl:wa:{chat.phone}:min", per_minute, 60
            )
            phone_ok_hour = await rate_limit.allow(
                self._deps.redis, f"rl:wa:{chat.phone}:hour", per_hour, 3600
            )
            if not (phone_ok_min and phone_ok_hour):
                for message in text_messages:
                    message.state = MsgState.IGNORED
                    message.error_code = "rate_limited"
                await db.commit()
                return _ClaimResult(media_ack=media_ack)

            if await self._is_spam(db, chat_id, text_messages, spam_threshold):
                for message in text_messages:
                    message.state = MsgState.IGNORED
                    message.error_code = "spam"
                await db.commit()
                return _ClaimResult(media_ack=media_ack)

            # 5. Claim: deterministic correlation + PROCESSING lease.
            correlation_id = correlation_for(chat_id, text_messages[-1].id)
            lease = now + _PROCESSING_LEASE
            for message in text_messages:
                message.state = MsgState.PROCESSING
                message.correlation_id = correlation_id
                message.next_attempt_at = lease

            question = "\n".join(message_text(m) for m in text_messages if message_text(m))
            language = detect_language(question)
            history = await self._history(db, chat_id, before_id=text_messages[0].id)
            memory_row = await db.get(ChatMemory, chat_id)
            memory = snapshot(memory_row)
            summary = memory_row.summary_text if memory_row else None
            retry_count = max(m.retry_count for m in text_messages)
            claim = _Claim(
                chat_id=chat_id,
                instance_id=chat.instance_id,
                wa_chat_id=chat.wa_chat_id,
                correlation_id=correlation_id,
                message_ids=[m.id for m in text_messages],
                retry_count=retry_count,
                language=language,
                # The LAST message of the batch is the one the answer
                # addresses, so that is what it quotes.
                quote_provider_message_id=text_messages[-1].provider_message_id,
                generation_input=GenerationInput(
                    correlation_id=correlation_id,
                    question=question,
                    language=language,
                    chat_id=chat_id,
                    phone=chat.phone,
                    history=history,
                    memory=memory,
                    summary=summary,
                    merged_count=len(text_messages),
                    attempt=retry_count,
                ),
            )
            await db.commit()
            return _ClaimResult(claim=claim, media_ack=media_ack)

    async def _is_spam(
        self,
        db: AsyncSession,
        chat_id: int,
        batch: list[Message],
        threshold: int,
    ) -> bool:
        """N identical customer messages in a row (SPEC §6.7.5).

        Compared on `message_text`, so five identical voice notes count as
        spam too — their words live in `transcript`, not `body`.
        """
        bodies = {normalize_query(message_text(m)) for m in batch}
        if len(bodies) != 1:
            return False
        if len(batch) >= threshold:
            return True
        needed = threshold - len(batch)
        previous = (
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.source == MsgSource.CUSTOMER,
                        Message.id < batch[0].id,
                        or_(Message.body.is_not(None), Message.transcript.is_not(None)),
                    )
                    .order_by(Message.id.desc())
                    .limit(needed)
                )
            )
            .scalars()
            .all()
        )
        if len(previous) < needed:
            return False
        target = next(iter(bodies))
        return all(normalize_query(message_text(m)) == target for m in previous)

    async def _history(
        self, db: AsyncSession, chat_id: int, *, before_id: int
    ) -> list[HistoryTurn]:
        rows = (
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.id < before_id,
                        # A voice note or photo carries its text in
                        # `transcript`; filtering on body alone would drop
                        # it from history and the saved image description
                        # would never be reused (SPEC §13).
                        or_(Message.body.is_not(None), Message.transcript.is_not(None)),
                    )
                    .order_by(Message.id.desc())
                    .limit(_HISTORY_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        turns: list[HistoryTurn] = []
        for message in reversed(rows):
            if message.source is MsgSource.CUSTOMER:
                role = "user"
            elif message.source in _HISTORY_ASSISTANT_SOURCES:
                role = "assistant"
            else:
                continue
            content = message_text(message)
            if not content:
                continue
            turns.append(HistoryTurn(role=role, content=content))
        return turns

    # ------------------------------ persist -----------------------------

    async def _persist(self, claim: _Claim, outcome: GenerationOutcome) -> None:  # noqa: PLR0912 - one branch per outcome kind
        now = datetime.now(UTC)
        async with self._deps.sessionmaker() as db:
            chat = await db.get(Chat, claim.chat_id, with_for_update=True)
            messages = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.id.in_(claim.message_ids),
                            Message.state == MsgState.PROCESSING,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if chat is None or not messages:
                await db.commit()
                return

            if outcome.text is not None:
                # Invariant 3: GENERATED (with text) BEFORE any send.
                reply_id = await self._insert_reply(
                    db,
                    claim.chat_id,
                    claim.correlation_id,
                    outcome.text,
                    answer_source_of(outcome.kind),
                )
                await enqueue_text(
                    db,
                    claim.instance_id,
                    claim.wa_chat_id,
                    outcome.text,
                    reply_outbound_id(claim.correlation_id),
                    claim.correlation_id,
                    quoted_message_id=(
                        claim.quote_provider_message_id
                        if await get_setting(db, app_settings.QUOTE_CUSTOMER_MESSAGE)
                        else None
                    ),
                )
                terminal = (
                    MsgState.BLOCKED_BY_LIMIT
                    if outcome.kind == "blocked_limit"
                    else MsgState.ANSWERED
                )
                for message in messages:
                    message.state = terminal
                    message.next_attempt_at = None
                    # Why the bot answered THIS way: without it a refusal
                    # and a real answer look identical in the reports, and
                    # "which limit stopped us" is lost entirely.
                    if outcome.error_code and outcome.kind != "model":
                        message.error_code = outcome.error_code
                chat.last_actor = LastActor.AI
                chat.last_message_at = now
                if outcome.paid:
                    # недочёт №7: the dialog's running cost is real now.
                    chat.total_cost_usd = chat.total_cost_usd + outcome.cost_usd
                # The model has no way to call a manager (no tools) and an
                # FAQ answer never sees the model: a request for a person
                # or a booking is recognised HERE, from the customer's own
                # words, or the manager would never learn about it.
                escalation = escalation_reason(claim.generation_input.question)
                if outcome.kind == "blocked_limit":
                    await set_attention(
                        db, chat, AttentionReason.BLOCKED_BY_LIMIT, claim.correlation_id
                    )
                elif outcome.handoff or outcome.kind == "no_data" or escalation is not None:
                    await set_attention(db, chat, AttentionReason.HANDOFF, claim.correlation_id)
                await remember(
                    db,
                    claim.chat_id,
                    text=claim.generation_input.question,
                    language=claim.language,
                    handoff_required=True if escalation is not None else None,
                )
                if outcome.ai_call_id is not None and reply_id is not None:
                    await db.execute(
                        update(AiCall)
                        .where(AiCall.id == outcome.ai_call_id)
                        .values(message_id=reply_id)
                    )
            elif outcome.kind == "breaker_open":
                # Not this batch's fault: retry later, no retry_count spent.
                retry_at = now + timedelta(
                    minutes=int(
                        await get_setting(db, app_settings.CIRCUIT_BREAKER_COOLDOWN_MINUTES)
                    )
                )
                for message in messages:
                    message.state = MsgState.QUEUED
                    message.next_attempt_at = retry_at
            elif outcome.kind == "not_configured":
                # Honest skip: no instruction/key/prices yet. Requeue on a
                # slow cadence and tell the owner (throttled + attention).
                for message in messages:
                    message.state = MsgState.QUEUED
                    message.next_attempt_at = now + _NOT_CONFIGURED_RETRY
                await self._notify_not_configured(db, chat, outcome.error_code or "")
            elif outcome.retryable and claim.retry_count < self._max_retries() - 1:
                backoff = timedelta(seconds=_RETRY_BACKOFF_BASE_SECONDS * (2**claim.retry_count))
                for message in messages:
                    message.state = MsgState.QUEUED
                    message.retry_count = message.retry_count + 1
                    message.next_attempt_at = now + backoff
                    message.error_code = outcome.error_code
            else:
                for message in messages:
                    message.state = MsgState.FAILED_FINAL
                    message.error_code = outcome.error_code
                    message.next_attempt_at = None
                await set_attention(
                    db, chat, AttentionReason.PROCESSING_ERROR, claim.correlation_id
                )
            await db.commit()

    def _max_retries(self) -> int:
        return self._deps.settings.retry_max_attempts

    async def _insert_reply(
        self,
        db: AsyncSession,
        chat_id: int,
        correlation_id: str,
        text: str,
        source: AnswerSource,
    ) -> int | None:
        outbound_id = reply_outbound_id(correlation_id)
        stmt = (
            pg_insert(Message)
            .values(
                chat_id=chat_id,
                direction=MsgDirection.OUT,
                source=MsgSource.AI,
                outbound_message_id=outbound_id,
                body=text,
                state=MsgState.GENERATED,
                correlation_id=correlation_id,
                # The only record that a free answer happened: FAQ and
                # cache hits never reach ai_calls (nothing was paid).
                answer_source=source,
            )
            .on_conflict_do_nothing(
                index_elements=["outbound_message_id"],
                index_where=sa_text("outbound_message_id IS NOT NULL"),
            )
            .returning(Message.id)
        )
        inserted = (await db.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return int(inserted)
        existing = (
            await db.execute(select(Message.id).where(Message.outbound_message_id == outbound_id))
        ).scalar_one_or_none()
        return int(existing) if existing is not None else None

    async def _plan_media_ack(
        self, db: AsyncSession, chat: Chat, media_messages: list[Message]
    ) -> _MediaAck:
        """Collect what is needed to acknowledge an unreadable file.

        Only collection happens here: the wording is written by the
        model (owner decision 2026-09-11 — no canned templates), and a
        model call must not run while the claim transaction holds a lock
        on the chat row.
        """
        captions = "\n".join((m.body or "").strip() for m in media_messages if m.body)
        memory = await db.get(ChatMemory, chat.id)
        if memory is not None and memory.language in ("ru", "kk", "en"):
            language = KbLanguage(memory.language)
        else:
            language = detect_language(captions)
        instruction = await get_active_instruction(db)
        return _MediaAck(
            chat_id=chat.id,
            instance_id=chat.instance_id,
            wa_chat_id=chat.wa_chat_id,
            phone=chat.phone,
            correlation_id=f"chat:{chat.id}:media:{media_messages[-1].id}",
            language=language,
            style=instruction.style if instruction else None,
            # If ANY of the batch is a real file, the file wording is the
            # honest one; a lone location or contact gets its own.
            reason=(
                "media"
                if any(m.media_type is not MediaType.OTHER for m in media_messages)
                else "unsupported_content"
            ),
        )

    async def _send_media_ack(self, ack: _MediaAck) -> None:
        """Tell the customer their file arrived, in their language.

        Idempotent through the deterministic outbound id: a re-run
        cannot send it twice.
        """
        reply = await compose_service_reply(
            self._deps,
            reason=ack.reason,
            language=ack.language,
            correlation_id=ack.correlation_id,
            chat_id=ack.chat_id,
            # Without the phone the per-user daily budget scope is
            # skipped and this spend escapes that limit.
            phone=ack.phone,
            style=ack.style,
        )
        async with self._deps.sessionmaker() as db:
            reply_id = await self._insert_reply(
                db, ack.chat_id, ack.correlation_id, reply.text, AnswerSource.SERVICE
            )
            if reply_id is not None:
                await enqueue_text(
                    db,
                    ack.instance_id,
                    ack.wa_chat_id,
                    reply.text,
                    reply_outbound_id(ack.correlation_id),
                    ack.correlation_id,
                )
            await db.commit()

    async def _notify_not_configured(self, db: AsyncSession, chat: Chat, error_code: str) -> None:
        throttle_key = "ai:event:not_configured"
        first = await self._deps.redis.set(throttle_key, "1", nx=True, ex=3600)
        if first:
            db.add(
                SystemEvent(
                    level="warning",
                    component="ai_pipeline",
                    event="ai_not_configured",
                    detail={"reason": error_code},
                )
            )
            # Customers are writing and nobody is answering them: without
            # an alert this state is only visible to someone who opens
            # the panel and knows where to look.
            await record_alert(
                db,
                kind="system",
                event_key="not_configured",
                period_key=hour_bucket(),
                detail={"reason": error_code},
            )
            await set_attention(db, chat, AttentionReason.PROCESSING_ERROR)

    # ------------------------------ sweeper -----------------------------

    async def sweep_processing(self) -> int:
        """Recover PROCESSING orphans whose lease expired (crash between
        claim and persist). If the GENERATED reply exists, finish the
        bookkeeping; otherwise re-queue with a retry spent."""
        now = datetime.now(UTC)
        recovered = 0
        async with self._deps.sessionmaker() as db:
            orphans = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.state == MsgState.PROCESSING,
                            Message.next_attempt_at.is_not(None),
                            Message.next_attempt_at < now,
                        )
                        .order_by(Message.id)
                        .limit(100)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            # Grouped by (correlation, chat): orphans with no correlation
            # would otherwise collapse into ONE group and the recovery
            # would mark an unrelated chat as needing attention.
            by_correlation: dict[tuple[str, int], list[Message]] = {}
            for message in orphans:
                key = (message.correlation_id or "", message.chat_id)
                by_correlation.setdefault(key, []).append(message)
            for (correlation_id, group_chat_id), group in by_correlation.items():
                chat = await db.get(Chat, group_chat_id, with_for_update=True)
                if chat is None:
                    continue
                reply = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.outbound_message_id == reply_outbound_id(correlation_id),
                            )
                        )
                    ).scalar_one_or_none()
                    if correlation_id
                    else None
                )
                if reply is not None and reply.body:
                    await enqueue_text(
                        db,
                        chat.instance_id,
                        chat.wa_chat_id,
                        reply.body,
                        reply_outbound_id(correlation_id),
                        correlation_id,
                    )
                    for message in group:
                        message.state = MsgState.ANSWERED
                        message.next_attempt_at = None
                    logger.warning("pipeline_orphan_finished", correlation_id=correlation_id)
                else:
                    for message in group:
                        if message.retry_count >= self._max_retries() - 1:
                            message.state = MsgState.FAILED_FINAL
                            message.error_code = "orphaned_processing"
                            message.next_attempt_at = None
                            await set_attention(
                                db, chat, AttentionReason.PROCESSING_ERROR, correlation_id
                            )
                        else:
                            message.state = MsgState.QUEUED
                            message.retry_count = message.retry_count + 1
                            message.next_attempt_at = now
                    logger.warning("pipeline_orphan_requeued", correlation_id=correlation_id)
                recovered += len(group)
            await db.commit()
        return recovered
