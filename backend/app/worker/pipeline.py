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
    AttentionReason,
    KbLanguage,
    LastActor,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.security import rate_limit
from app.services import app_settings
from app.services.app_settings import get_setting
from app.services.chat_memory import remember, snapshot
from app.services.chat_modes import ai_allowed
from app.services.chats import set_attention
from app.services.generation import GenerationInput, generate_reply
from app.services.kb_text import detect_language, normalize_query
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


def reply_outbound_id(correlation_id: str) -> uuid.UUID:
    return uuid.uuid5(REPLY_NAMESPACE, correlation_id)


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
                    )
                    .group_by(Message.chat_id)
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
            claim = await self._claim(chat_id)
            if claim is None:
                return 1
            outcome = await generate_reply(self._deps, claim.generation_input)
            await self._persist(claim, outcome)
            return 1
        finally:
            # Token-guarded release (best effort; the TTL is the backstop).
            current = await self._deps.redis.get(lock_key)
            holder = current.decode() if isinstance(current, bytes) else current
            if holder == token:
                await self._deps.redis.delete(lock_key)

    # ------------------------------ claim -------------------------------

    async def _claim(self, chat_id: int) -> _Claim | None:  # noqa: PLR0911, PLR0912, PLR0915 - the gate ladder IS the SPEC §6.7 order
        now = datetime.now(UTC)
        async with self._deps.sessionmaker() as db:
            chat = await db.get(Chat, chat_id, with_for_update=True)
            if chat is None:
                return None
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
                return None

            max_age_minutes = int(await get_setting(db, app_settings.AI_MAX_MESSAGE_AGE_MINUTES))
            emergency_off = bool(await get_setting(db, app_settings.EMERGENCY_AI_OFF))
            per_minute = int(await get_setting(db, app_settings.MAX_MESSAGES_PER_USER_PER_MINUTE))
            per_hour = int(await get_setting(db, app_settings.MAX_MESSAGES_PER_USER_PER_HOUR))
            spam_threshold = int(await get_setting(db, app_settings.SPAM_REPEAT_THRESHOLD))

            # 1. Stale cutoff (owner decision 2026-09-06: 1 hour): the bot
            # answering yesterday's message does more harm than silence.
            cutoff = now - timedelta(minutes=max_age_minutes)
            fresh: list[Message] = []
            stale_found = False
            for message in messages:
                if message.created_at < cutoff:
                    message.state = MsgState.IGNORED
                    message.error_code = "stale"
                    stale_found = True
                else:
                    fresh.append(message)
            if stale_found:
                await set_attention(db, chat, AttentionReason.HANDOFF)

            # 2. Media is a phase 7 feature — no stubs: hand it to a human.
            text_messages: list[Message] = []
            for message in fresh:
                if message.media_type is not None:
                    message.state = MsgState.HANDED_TO_HUMAN
                    message.error_code = "media_unsupported"
                    await set_attention(db, chat, AttentionReason.HANDOFF)
                elif not (message.body or "").strip():
                    message.state = MsgState.IGNORED
                    message.error_code = "empty"
                else:
                    text_messages.append(message)
            if not text_messages:
                await db.commit()
                return None

            # 3. THE mode gate (недочёт №5): emergency switch + chat mode.
            if not ai_allowed(chat, emergency_off=emergency_off):
                for message in text_messages:
                    message.state = MsgState.HANDED_TO_HUMAN
                    message.error_code = "ai_off"
                await db.commit()
                return None

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
                return None

            if await self._is_spam(db, chat_id, text_messages, spam_threshold):
                for message in text_messages:
                    message.state = MsgState.IGNORED
                    message.error_code = "spam"
                await db.commit()
                return None

            # 5. Claim: deterministic correlation + PROCESSING lease.
            correlation_id = f"chat:{chat_id}:msg:{text_messages[-1].id}"
            lease = now + _PROCESSING_LEASE
            for message in text_messages:
                message.state = MsgState.PROCESSING
                message.correlation_id = correlation_id
                message.next_attempt_at = lease

            question = "\n".join((m.body or "").strip() for m in text_messages)
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
            return claim

    async def _is_spam(
        self,
        db: AsyncSession,
        chat_id: int,
        batch: list[Message],
        threshold: int,
    ) -> bool:
        """N identical customer messages in a row (SPEC §6.7.5)."""
        bodies = {normalize_query(m.body or "") for m in batch}
        if len(bodies) != 1:
            return False
        if len(batch) >= threshold:
            return True
        needed = threshold - len(batch)
        previous = (
            (
                await db.execute(
                    select(Message.body)
                    .where(
                        Message.chat_id == chat_id,
                        Message.source == MsgSource.CUSTOMER,
                        Message.id < batch[0].id,
                        Message.body.is_not(None),
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
        return all(normalize_query(body or "") == target for body in previous)

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
                        Message.body.is_not(None),
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
            turns.append(HistoryTurn(role=role, content=message.body or ""))
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
                reply_id = await self._insert_reply(db, claim, outcome.text)
                await enqueue_text(
                    db,
                    claim.instance_id,
                    claim.wa_chat_id,
                    outcome.text,
                    reply_outbound_id(claim.correlation_id),
                    claim.correlation_id,
                )
                terminal = (
                    MsgState.BLOCKED_BY_LIMIT
                    if outcome.kind == "blocked_limit"
                    else MsgState.ANSWERED
                )
                for message in messages:
                    message.state = terminal
                    message.next_attempt_at = None
                chat.last_actor = LastActor.AI
                chat.last_message_at = now
                if outcome.paid:
                    # недочёт №7: the dialog's running cost is real now.
                    chat.total_cost_usd = chat.total_cost_usd + outcome.cost_usd
                if outcome.kind == "blocked_limit":
                    await set_attention(
                        db, chat, AttentionReason.BLOCKED_BY_LIMIT, claim.correlation_id
                    )
                elif outcome.handoff or outcome.kind == "no_data":
                    await set_attention(db, chat, AttentionReason.HANDOFF, claim.correlation_id)
                await remember(
                    db,
                    claim.chat_id,
                    text=claim.generation_input.question,
                    language=claim.language,
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

    async def _insert_reply(self, db: AsyncSession, claim: _Claim, text: str) -> int | None:
        stmt = (
            pg_insert(Message)
            .values(
                chat_id=claim.chat_id,
                direction=MsgDirection.OUT,
                source=MsgSource.AI,
                outbound_message_id=reply_outbound_id(claim.correlation_id),
                body=text,
                state=MsgState.GENERATED,
                correlation_id=claim.correlation_id,
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
            await db.execute(
                select(Message.id).where(
                    Message.outbound_message_id == reply_outbound_id(claim.correlation_id)
                )
            )
        ).scalar_one_or_none()
        return int(existing) if existing is not None else None

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
            by_correlation: dict[str, list[Message]] = {}
            for message in orphans:
                by_correlation.setdefault(message.correlation_id or "", []).append(message)
            for correlation_id, group in by_correlation.items():
                chat = await db.get(Chat, group[0].chat_id, with_for_update=True)
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
