"""Load test: 5 and 30 customers writing at once (SPEC §16, phase 10).

What is being proven, in the words of the SPEC: the queue grows, the
server stays alive, and the number of parallel model calls stays capped
(8). Everything runs in one process against a scratch database with the
fake providers from `offline.py`; the model stand-in sleeps for a
configurable time so that concurrency is real, not theoretical.

The chain is the production one, stage by stage: provider notification
-> inbox -> chat/message -> (media job for voice notes) -> AI pipeline
-> reply row -> outbox (DRY_RUN: skipped, never sent).
"""

from __future__ import annotations

import asyncio
import io
import statistics
import struct
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models import InboundNotification, MediaJob, Message, SystemEvent, WaOutbox
from app.models.enums import (
    AnswerSource,
    MediaJobState,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services.inbox_processing import process_inbound_batch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.eval.offline import OracleChatProvider
    from app.services.media_processing import MediaProcessor
    from app.whatsapp.outbox import OutboxFlusher
    from app.worker.pipeline import AiPipeline

#: A realistic mix: paid answers, free FAQ hits, other languages, a
#: booking that must reach a manager.
QUESTIONS = (
    "Сколько стоит абонемент в тренажерный зал?",
    "Аренда футбольного поля — сколько стоит час?",
    "Во сколько вы работаете?",
    "Какой у вас адрес?",
    "Есть ли секция бокса и когда занятия?",
    "Сколько стоит койко-место в гостинице?",
    "Абонемент қанша тұрады?",
    "How much is a gym membership?",
    "Можно заморозить абонемент на месяц?",
    "Хочу забронировать игровой зал на субботу в 18:00",
)
VOICE_TRANSCRIPT = "здравствуйте сколько стоит абонемент в тренажерный зал на месяц"
#: SPEC §9.3 / §16: the pipeline's global semaphore.
MAX_CONCURRENT_AI_REQUESTS = 8
_SETTLE_POLL_SECONDS = 0.2
_PROBE_INTERVAL_SECONDS = 0.1


def make_wav(seconds: float, *, rate: int = 8000) -> bytes:
    """Silent PCM WAV: mutagen reads its duration, so the media path runs
    its real length check before the (fake) transcription."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class LoadRound:
    label: str
    chats: int
    messages_in: int
    voice_notes: int
    replies: int
    answered_by_model: int
    answered_by_faq: int
    answered_by_service: int
    handed_to_human: int
    unanswered: int
    duplicates: int
    order_violations: int
    transcribed: int
    max_model_concurrency: int
    pool_max_checked_out: int
    wall_seconds: float
    messages_per_second: float
    wait_ms_p50: float
    wait_ms_p95: float
    wait_ms_max: float
    probe_count: int
    probe_ms_p50: float
    probe_ms_p95: float
    probe_ms_max: float
    probe_failures: int
    error_events: int
    outbox: dict[str, int]
    completed: bool

    @property
    def ok(self) -> bool:
        return (
            self.completed
            and self.unanswered == 0
            and self.duplicates == 0
            and self.order_violations == 0
            and self.max_model_concurrency <= MAX_CONCURRENT_AI_REQUESTS
            and self.error_events == 0
            and self.probe_failures == 0
        )


@dataclass
class LoadReport:
    started_at: datetime
    model_latency_seconds: float
    rounds: list[LoadRound] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rounds) and all(r.ok for r in self.rounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "model_latency_seconds": self.model_latency_seconds,
            "ok": self.ok,
            "rounds": [asdict(r) | {"ok": r.ok} for r in self.rounds],
            "notes": self.notes,
        }

    def render_markdown(self) -> str:
        lines = [
            "# Отчёт нагрузочной проверки",
            "",
            f"- Начало: {self.started_at:%Y-%m-%d %H:%M} UTC",
            f"- Имитация задержки модели: {self.model_latency_seconds:.1f} с на вызов",
            f"- Итог: **{'пройдено' if self.ok else 'НЕ пройдено'}**",
            "",
            "| Раунд | Чатов | Сообщений | Голосовых | Ответов | Модель | FAQ | Сервисных | "
            "Менеджеру | Без ответа | Дубли | Порядок нарушен | Макс. параллельных вызовов | "
            "Макс. соединений БД | Время, с | Сообщ./с | Ожидание p50/p95/max, мс | "
            "Панель p50/p95/max, мс | Ошибок | Итог |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in self.rounds:
            waits = f"{r.wait_ms_p50:.0f}/{r.wait_ms_p95:.0f}/{r.wait_ms_max:.0f}"
            probes = f"{r.probe_ms_p50:.0f}/{r.probe_ms_p95:.0f}/{r.probe_ms_max:.0f}"
            lines.append(
                f"| {r.label} | {r.chats} | {r.messages_in} | {r.voice_notes} | {r.replies} | "
                f"{r.answered_by_model} | {r.answered_by_faq} | {r.answered_by_service} | "
                f"{r.handed_to_human} | {r.unanswered} | {r.duplicates} | {r.order_violations} | "
                f"{r.max_model_concurrency} | {r.pool_max_checked_out} | {r.wall_seconds:.1f} | "
                f"{r.messages_per_second:.1f} | {waits} | {probes} ({r.probe_count}) | "
                f"{r.error_events} | {'✅' if r.ok else '❌'} |"
            )
        lines += ["", "## Что проверялось", ""]
        lines += [
            "- Полная цепочка: уведомление провайдера → входящая очередь → чат и сообщение → "
            "(голосовое: задача медиа, проверка длительности, транскрибация) → конвейер ИИ → "
            "ответ в БД → outbox (DRY_RUN: помечено, не отправлено).",
            "- «Макс. параллельных вызовов» — сколько вызовов модели шло одновременно; "
            "предел SPEC §16 — 8.",
            "- «Порядок нарушен» — второй раунд шлёт в каждый чат ещё одно сообщение; "
            "ответ на него должен появиться после ответа на первое.",
            "- «Панель» — задержка GET /api/chats из того же процесса во время нагрузки: "
            "показывает, что цикл событий не блокируется; в проде API и воркер — "
            "разные контейнеры.",
            "- Outbox: в режиме DRY_RUN все ответы клиентам получают состояние SKIPPED_DRY_RUN.",
        ]
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n"


def _text_payload(id_message: str, chat_id: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": id_message,
        "timestamp": timestamp,
        "senderData": {"chatId": chat_id, "senderName": "Нагрузка"},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": text}},
    }


def _voice_payload(id_message: str, chat_id: str, timestamp: int, url: str) -> dict[str, Any]:
    return {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": id_message,
        "timestamp": timestamp,
        "senderData": {"chatId": chat_id, "senderName": "Нагрузка"},
        "messageData": {
            "typeMessage": "audioMessage",
            "fileMessageData": {
                "downloadUrl": url,
                "mimeType": "audio/wav",
                "fileName": "voice.wav",
                "caption": "",
            },
        },
    }


class LoadHarness:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        instance_pk: int,
        chat_provider: OracleChatProvider,
        pipeline: AiPipeline,
        media_processor: MediaProcessor,
        outbox: OutboxFlusher,
        probe: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._engine = engine
        self._instance_pk = instance_pk
        self._chat_provider = chat_provider
        self._pipeline = pipeline
        self._media = media_processor
        self._outbox = outbox
        self._probe = probe
        #: One harness may run several sizes (5, then 30); notification
        #: ids must stay unique across them or the inbox dedupe fires.
        self._runs = 0

    def _wa_chat_id(self, index: int) -> str:
        return f"7700{index:07d}@c.us"

    async def _inject(self, round_no: int, chats: int, voice_every: int) -> tuple[int, int]:
        now = int(time.time())
        voice = 0
        async with self._sessionmaker() as db, db.begin():
            for index in range(chats):
                chat_id = self._wa_chat_id(index)
                id_message = f"LOAD-{self._runs}-{round_no}-{index}"
                if voice_every and index % voice_every == 0:
                    voice += 1
                    payload = _voice_payload(
                        id_message,
                        chat_id,
                        now,
                        f"https://media.green-api.com/load/{round_no}-{index}.wav",
                    )
                else:
                    question = QUESTIONS[(index + round_no) % len(QUESTIONS)]
                    payload = _text_payload(id_message, chat_id, question, now)
                db.add(
                    InboundNotification(
                        instance_id=self._instance_pk,
                        provider_message_id=id_message,
                        notification_type="incomingMessageReceived",
                        raw_payload=payload,
                        status="stored",
                    )
                )
        return chats, voice

    async def _pending(self) -> bool:
        async with self._sessionmaker() as db:
            stored = (
                await db.execute(
                    select(func.count())
                    .select_from(InboundNotification)
                    .where(InboundNotification.status == "stored")
                )
            ).scalar_one()
            open_messages = (
                await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.direction == MsgDirection.IN,
                        Message.source == MsgSource.CUSTOMER,
                        Message.state.in_((MsgState.QUEUED, MsgState.PROCESSING)),
                    )
                )
            ).scalar_one()
            open_jobs = (
                await db.execute(
                    select(func.count())
                    .select_from(MediaJob)
                    .where(MediaJob.state.in_((MediaJobState.PENDING, MediaJobState.PROCESSING)))
                )
            ).scalar_one()
            outbox_open = (
                await db.execute(
                    select(func.count())
                    .select_from(WaOutbox)
                    .where(WaOutbox.state.in_(("PENDING", "FAILED_RETRYABLE")))
                )
            ).scalar_one()
        return bool(stored or open_messages or open_jobs or outbox_open)

    async def _drive(self, timeout_seconds: float) -> tuple[bool, int, list[float], int]:
        """Run every worker stage until the system is quiet or time is up.

        Returns (completed, pool_max_checked_out, probe latencies ms,
        probe failures).
        """
        done = asyncio.Event()
        pool_max = 0
        probe_ms: list[float] = []
        probe_failures = 0

        async def stage(step: Callable[[], Awaitable[int]], idle: float) -> None:
            while not done.is_set():
                handled = await step()
                if handled == 0:
                    await asyncio.sleep(idle)

        async def inbox_step() -> int:
            async with self._sessionmaker() as db:
                handled = await process_inbound_batch(db)
                await db.commit()
            return handled

        async def sampler() -> None:
            nonlocal pool_max
            while not done.is_set():
                pool_max = max(pool_max, self._engine.pool.checkedout())  # type: ignore[attr-defined]
                await asyncio.sleep(0.05)

        async def prober() -> None:
            nonlocal probe_failures
            if self._probe is None:
                return
            while not done.is_set():
                started = time.perf_counter()
                ok = await self._probe()
                probe_ms.append((time.perf_counter() - started) * 1000)
                if not ok:
                    probe_failures += 1
                await asyncio.sleep(_PROBE_INTERVAL_SECONDS)

        async def watcher() -> bool:
            deadline = time.monotonic() + timeout_seconds
            # Give the inbox one pass before the first quiet check.
            await asyncio.sleep(_SETTLE_POLL_SECONDS)
            while time.monotonic() < deadline:
                if not await self._pending():
                    done.set()
                    return True
                await asyncio.sleep(_SETTLE_POLL_SECONDS)
            done.set()
            return False

        async with asyncio.TaskGroup() as group:
            group.create_task(stage(inbox_step, 0.05))
            group.create_task(stage(self._media.run_once, 0.05))
            group.create_task(stage(self._pipeline.run_once, 0.05))
            group.create_task(stage(self._outbox.flush_once, 0.05))
            group.create_task(sampler())
            group.create_task(prober())
            watch = group.create_task(watcher())
        return watch.result(), pool_max, probe_ms, probe_failures

    async def _measure(  # noqa: PLR0914 - one local per reported number
        self,
        label: str,
        *,
        chats: int,
        messages_in: int,
        voice_notes: int,
        since: datetime,
        wall_seconds: float,
        completed: bool,
        pool_max: int,
        probe_ms: list[float],
        probe_failures: int,
        previous_replies: dict[int, datetime],
    ) -> tuple[LoadRound, dict[int, datetime]]:
        async with self._sessionmaker() as db:
            incoming = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.direction == MsgDirection.IN,
                            Message.source == MsgSource.CUSTOMER,
                            Message.created_at >= since,
                        )
                    )
                )
                .scalars()
                .all()
            )
            replies = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.direction == MsgDirection.OUT,
                            Message.source == MsgSource.AI,
                            Message.created_at >= since,
                        )
                    )
                )
                .scalars()
                .all()
            )
            outbox_rows = (
                await db.execute(
                    select(WaOutbox.state, func.count())
                    .where(WaOutbox.created_at >= since)
                    .group_by(WaOutbox.state)
                )
            ).all()
            error_events = (
                await db.execute(
                    select(func.count())
                    .select_from(SystemEvent)
                    .where(
                        SystemEvent.level.in_(("error", "critical")),
                        SystemEvent.created_at >= since,
                    )
                )
            ).scalar_one()
            handed = (
                await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.direction == MsgDirection.IN,
                        Message.created_at >= since,
                        Message.state == MsgState.HANDED_TO_HUMAN,
                    )
                )
            ).scalar_one()

        by_chat_in = {m.chat_id: m for m in incoming}
        replies_by_chat: dict[int, list[Message]] = {}
        for reply in replies:
            replies_by_chat.setdefault(reply.chat_id, []).append(reply)
        waits = [
            (rows[0].created_at - by_chat_in[chat_id].created_at).total_seconds() * 1000
            for chat_id, rows in replies_by_chat.items()
            if chat_id in by_chat_in
        ]
        unanswered = sum(
            1
            for chat_id, message in by_chat_in.items()
            if chat_id not in replies_by_chat and message.state is not MsgState.HANDED_TO_HUMAN
        )
        duplicates = sum(1 for rows in replies_by_chat.values() if len(rows) > 1)
        order_violations = sum(
            1
            for chat_id, rows in replies_by_chat.items()
            if chat_id in previous_replies and rows[0].created_at <= previous_replies[chat_id]
        )
        transcribed = sum(1 for m in incoming if m.transcript)
        outbox = {state: int(count) for state, count in outbox_rows}
        round_result = LoadRound(
            label=label,
            chats=chats,
            messages_in=messages_in,
            voice_notes=voice_notes,
            replies=len(replies),
            answered_by_model=sum(1 for r in replies if r.answer_source is AnswerSource.MODEL),
            answered_by_faq=sum(1 for r in replies if r.answer_source is AnswerSource.FAQ),
            answered_by_service=sum(1 for r in replies if r.answer_source is AnswerSource.SERVICE),
            handed_to_human=int(handed),
            unanswered=unanswered,
            duplicates=duplicates,
            order_violations=order_violations,
            transcribed=transcribed,
            max_model_concurrency=self._chat_provider.max_in_flight,
            pool_max_checked_out=pool_max,
            wall_seconds=wall_seconds,
            messages_per_second=(messages_in / wall_seconds) if wall_seconds else 0.0,
            wait_ms_p50=statistics.median(waits) if waits else 0.0,
            wait_ms_p95=_percentile(waits, 0.95),
            wait_ms_max=max(waits) if waits else 0.0,
            probe_count=len(probe_ms),
            probe_ms_p50=statistics.median(probe_ms) if probe_ms else 0.0,
            probe_ms_p95=_percentile(probe_ms, 0.95),
            probe_ms_max=max(probe_ms) if probe_ms else 0.0,
            probe_failures=probe_failures,
            error_events=int(error_events),
            outbox=outbox,
            completed=completed,
        )
        latest = {chat_id: rows[-1].created_at for chat_id, rows in replies_by_chat.items()}
        return round_result, latest

    async def run(
        self, chats: int, *, voice_every: int = 5, rounds: int = 2, timeout_seconds: float = 180.0
    ) -> list[LoadRound]:
        """`rounds` waves of one message per chat; the second wave checks
        that answers stay in order per chat."""
        results: list[LoadRound] = []
        previous: dict[int, datetime] = {}
        self._runs += 1
        for round_no in range(1, rounds + 1):
            self._chat_provider.max_in_flight = 0
            since = datetime.now(UTC)
            started = time.perf_counter()
            messages_in, voice_notes = await self._inject(round_no, chats, voice_every)
            completed, pool_max, probe_ms, probe_failures = await self._drive(timeout_seconds)
            wall = time.perf_counter() - started
            round_result, previous = await self._measure(
                f"{chats} чатов, волна {round_no}",
                chats=chats,
                messages_in=messages_in,
                voice_notes=voice_notes,
                since=since,
                wall_seconds=wall,
                completed=completed,
                pool_max=pool_max,
                probe_ms=probe_ms,
                probe_failures=probe_failures,
                previous_replies=previous,
            )
            results.append(round_result)
        return results
