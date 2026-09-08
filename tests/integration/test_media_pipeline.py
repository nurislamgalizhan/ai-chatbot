"""Media pipeline against real PostgreSQL: money barriers, refusals, order.

What matters here is not that transcription "works" (a fake provider
returns text) but that everything AROUND the paid call is right:
- nothing is paid for before every free check has passed;
- the same file can never be paid for twice, even after a crash;
- a sensitive photo, a too-long voice note or a disabled switch always
  reach a human WITH a reply to the customer, never silence;
- a chat waiting on a voice note is not answered out of order, and the
  worker loop does not spin on it.
"""

from __future__ import annotations

import io
import struct
import wave
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.ai.chat_provider import ChatError, FakeChatProvider, make_result
from app.ai.transcription import (
    FakeTranscriptionProvider,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.media import storage
from app.models import AiCall, BudgetLedger, Chat, MediaJob, Message, WaOutbox
from app.models.enums import (
    BudgetScope,
    ChatMode,
    MediaJobState,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services import app_settings
from app.services.app_settings import set_setting
from app.services.generation import DEFAULT_TEMPLATES
from app.services.media_processing import MediaDeps, MediaProcessor, enqueue_media_job
from app.services.pricing import seed_default_prices
from app.whatsapp.provider import ProviderError, WhatsAppProvider
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TRANSCRIPT = "Здравствуйте, сколько стоит абонемент в тренажерный зал?"
DESCRIPTION = "На фото стойка администратора с прайс-листом."
DOWNLOAD_URL = "https://media.green-api.com/file/voice-1.ogg"


def make_wav(seconds: float, *, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


def make_jpeg(width: int = 800, height: int = 600) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(30, 60, 90)).save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeDownloadProvider(WhatsAppProvider):
    """Only the download half of the transport is exercised here."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    async def download_file(self, url: str, max_bytes: int) -> bytes:
        self.calls.append((url, max_bytes))
        if self.error is not None:
            raise self.error
        if len(self.payload) > max_bytes:
            raise ProviderError(
                "downloadFile.too_large", status_code=None, retryable=False
            )
        return self.payload

    async def poll_notification(self):  # pragma: no cover - unused
        return None

    async def delete_notification(self, receipt_id: int) -> None:  # pragma: no cover
        return None

    async def send_text(self, chat_id: str, text: str):  # pragma: no cover
        raise NotImplementedError

    async def send_file(
        self, chat_id, file_path, file_name, caption=None
    ):  # pragma: no cover
        raise NotImplementedError

    async def get_instance_state(self) -> str:  # pragma: no cover
        return "authorized"

    async def aclose(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
async def instance_pk(db_engine) -> int:
    from app.models import WhatsappInstance

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        row = WhatsappInstance(
            provider="green_api", instance_id="1101000001", token_ref="GREEN_API_TOKEN"
        )
        db.add(row)
        await db.flush()
        return row.id


@pytest.fixture
async def media_env(db_engine, settings, instance_pk, tmp_path):
    """Prices, rate and a media root on a temp dir (never /data in tests)."""
    from app.models import FxRate

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(
            FxRate(pair="USD/KZT", rate=Decimal(500), effective_from=datetime.now(UTC))
        )
        await seed_default_prices(db, settings)
        # Vision is off by default (PII policy); the image tests turn it on.
        await set_setting(db, app_settings.VISION_ENABLED, True)

    media_settings = settings.model_copy(update={"media_dir": str(tmp_path)})
    transcription = FakeTranscriptionProvider(
        script=[
            TranscriptionResult(text=TRANSCRIPT, model="gpt-transcribe", latency_ms=120)
        ]
    )
    chat_provider = FakeChatProvider(
        vision_script=[make_result(DESCRIPTION, input_tokens=1500)]
    )
    download = FakeDownloadProvider(make_wav(8.0))
    deps = MediaDeps(
        sessionmaker=maker,
        settings=media_settings,
        redis=None,  # the media path takes no Redis locks
        provider=download,
        chat_provider=chat_provider,
        transcription_provider=transcription,
    )
    return {
        "maker": maker,
        "settings": media_settings,
        "processor": MediaProcessor(deps),
        "deps": deps,
        "download": download,
        "transcription": transcription,
        "chat_provider": chat_provider,
        "instance_pk": instance_pk,
        "media_dir": str(tmp_path),
    }


async def make_chat(
    maker, instance_pk: int, *, mode: ChatMode = ChatMode.AUTO_AI
) -> int:
    async with maker() as db, db.begin():
        chat = Chat(
            instance_id=instance_pk,
            wa_chat_id="77011234567@c.us",
            phone="+77011234567",
            mode=mode,
        )
        db.add(chat)
        await db.flush()
        return chat.id


async def add_media_message(
    maker,
    chat_id: int,
    *,
    media_type: MediaType = MediaType.VOICE,
    caption: str | None = None,
    sent_age_minutes: int = 0,
    url: str | None = DOWNLOAD_URL,
    mime_type: str = "audio/ogg",
) -> int:
    async with maker() as db, db.begin():
        message = Message(
            chat_id=chat_id,
            direction=MsgDirection.IN,
            source=MsgSource.CUSTOMER,
            provider_message_id=f"pm-{chat_id}-{datetime.now(UTC).timestamp()}",
            body=caption,
            media_type=media_type,
            state=MsgState.QUEUED,
            sent_at=datetime.now(UTC) - timedelta(minutes=sent_age_minutes),
        )
        db.add(message)
        await db.flush()
        await enqueue_media_job(
            db,
            message_id=message.id,
            chat_id=chat_id,
            media_type=media_type,
            download_url=url,
            mime_type=mime_type,
        )
        return message.id


async def get_message(maker, message_id: int) -> Message:
    async with maker() as db:
        return (
            await db.execute(select(Message).where(Message.id == message_id))
        ).scalar_one()


async def get_job(maker, message_id: int) -> MediaJob:
    async with maker() as db:
        return (
            await db.execute(select(MediaJob).where(MediaJob.message_id == message_id))
        ).scalar_one()


async def get_replies(maker, chat_id: int) -> list[Message]:
    async with maker() as db:
        return list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.direction == MsgDirection.OUT,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )


# ------------------------------ happy path ------------------------------


async def test_voice_becomes_text_and_is_billed_by_duration(media_env) -> None:
    """A voice note turns into a QUEUED text message the AI pipeline can
    answer, with the cost booked by seconds of audio."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    assert await media_env["processor"].run_once() == 1

    message = await get_message(maker, message_id)
    assert message.transcript == TRANSCRIPT
    # Still QUEUED: answering it is the AI pipeline's job, not ours.
    assert message.state is MsgState.QUEUED
    assert message.media_ref is not None
    assert message.correlation_id == f"chat:{chat_id}:media:{message_id}"

    job = await get_job(maker, message_id)
    assert job.state is MediaJobState.DONE
    assert job.duration_seconds == Decimal("8.00")
    assert job.sha256 and job.file_bytes

    async with maker() as db:
        call = (await db.execute(select(AiCall))).scalars().one()
        chat = await db.get(Chat, chat_id)
        day = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
    # 8 s of audio at $0.0045/min = $0.0006
    assert call.audio_seconds == 8
    assert call.cost_usd == Decimal("0.00060000")
    assert call.cost_kzt == Decimal("0.3000")  # frozen at rate 500
    assert call.status == "ok"
    assert chat.total_cost_usd == Decimal("0.000600")  # media spending is real spending
    assert day.spent_usd > 0
    assert day.reserved_usd == 0  # settle freed the estimate

    # The stored file is readable through the safe-path resolver.
    assert storage.read_bytes(media_env["media_dir"], message.media_ref)


async def test_terms_and_language_are_passed_to_transcription(media_env) -> None:
    maker = media_env["maker"]
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.TRANSCRIPTION_TERMS, "Меркурий Медет, самбо")
    chat_id = await make_chat(maker, media_env["instance_pk"])
    await add_media_message(maker, chat_id, caption="сәлем")

    await media_env["processor"].run_once()

    request: TranscriptionRequest = media_env["transcription"].requests[0]
    assert request.terms == "Меркурий Медет, самбо"
    assert request.language == "kk"  # from the caption, not a default


# --------------------------- free refusals ------------------------------


async def test_too_long_voice_is_refused_before_paying(media_env) -> None:
    """The duration is measured locally, so the refusal costs nothing —
    and the customer gets the owner's template, not silence."""
    maker = media_env["maker"]
    media_env["download"].payload = make_wav(200.0)  # over the 120 s limit
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "audio_too_long"
    assert media_env["transcription"].requests == []  # nothing paid

    replies = await get_replies(maker, chat_id)
    assert [r.body for r in replies] == [DEFAULT_TEMPLATES["audio_too_long"]["ru"]]
    async with maker() as db:
        assert (await db.execute(select(AiCall))).scalars().all() == []
        chat = await db.get(Chat, chat_id)
        outbox = (await db.execute(select(WaOutbox))).scalars().all()
    assert chat.needs_attention is True
    assert len(outbox) == 1  # the refusal is actually queued for sending


@pytest.mark.parametrize(
    "caption",
    ["вот мой паспорт", "моя карта для оплаты", "справка от врача"],
)
async def test_sensitive_photo_never_reaches_openai(media_env, caption: str) -> None:
    """SPEC §13: an ID, a bank card or a medical document must not be
    analyzed automatically. The file is kept for the human to look at."""
    maker = media_env["maker"]
    media_env["download"].payload = make_jpeg()
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(
        maker,
        chat_id,
        media_type=MediaType.IMAGE,
        caption=caption,
        mime_type="image/jpeg",
    )

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code.startswith("sensitive:")
    assert message.transcript is None
    assert media_env["chat_provider"].vision_requests == []  # the image never left
    replies = await get_replies(maker, chat_id)
    assert [r.body for r in replies] == [DEFAULT_TEMPLATES["media_sensitive"]["ru"]]


async def test_vision_disabled_hands_photo_to_human_with_a_reply(media_env) -> None:
    maker = media_env["maker"]
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.VISION_ENABLED, False)
    media_env["download"].payload = make_jpeg()
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(
        maker, chat_id, media_type=MediaType.IMAGE, mime_type="image/jpeg"
    )

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "media_disabled"
    assert media_env["download"].calls == []  # not even downloaded
    assert [r.body for r in await get_replies(maker, chat_id)] == [
        DEFAULT_TEMPLATES["media"]["ru"]
    ]


async def test_stale_voice_is_refused_before_download(media_env) -> None:
    """Owner decision (1 hour): after an outage the bot must not pay to
    transcribe yesterday's voice note."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id, sent_age_minutes=180)

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.IGNORED
    assert message.error_code == "stale"
    assert media_env["download"].calls == []
    assert media_env["transcription"].requests == []
    async with maker() as db:
        chat = await db.get(Chat, chat_id)
    assert chat.needs_attention is True


@pytest.mark.parametrize("mode", [ChatMode.HUMAN, ChatMode.DISABLED])
async def test_mode_gate_blocks_media_for_free(media_env, mode) -> None:
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"], mode=mode)
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "ai_off"
    assert media_env["download"].calls == []
    # The bot may not speak in this chat, so not even a template goes out.
    assert await get_replies(maker, chat_id) == []


async def test_oversized_file_is_refused(media_env) -> None:
    maker = media_env["maker"]
    media_env["download"].payload = b"x" * (20 * 1024 * 1024)  # over 16 MB
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "media_too_large"
    assert media_env["transcription"].requests == []


async def test_image_limit_per_dialog_stops_a_photo_flood(media_env) -> None:
    maker = media_env["maker"]
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.MAX_IMAGES_PER_DIALOG, 1)
    media_env["download"].payload = make_jpeg()
    media_env["chat_provider"].vision_script = [
        make_result(DESCRIPTION, input_tokens=1500),
        make_result("вторая картинка", input_tokens=1500),
    ]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    first = await add_media_message(
        maker, chat_id, media_type=MediaType.IMAGE, mime_type="image/jpeg"
    )
    await media_env["processor"].run_once()
    assert (await get_message(maker, first)).transcript == DESCRIPTION

    second = await add_media_message(
        maker, chat_id, media_type=MediaType.IMAGE, mime_type="image/jpeg"
    )
    await media_env["processor"].run_once()

    message = await get_message(maker, second)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "image_limit_reached"
    assert (
        len(media_env["chat_provider"].vision_requests) == 1
    )  # only the first was paid


# ------------------------------ money safety ----------------------------


async def test_same_file_is_never_paid_for_twice(media_env) -> None:
    """The DB barrier (partial UNIQUE on ai_calls) is what makes this
    safe: after a crash that lost the transcript, the retry must NOT pay
    again — it hands the message to a human instead."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()
    assert len(media_env["transcription"].requests) == 1

    # Simulate the crash: the paid call happened (ai_calls row survives),
    # but the transcript never made it, and the job is claimable again.
    async with maker() as db, db.begin():
        message = await db.get(Message, message_id)
        message.transcript = None
        job = (
            await db.execute(select(MediaJob).where(MediaJob.message_id == message_id))
        ).scalar_one()
        job.state = MediaJobState.PENDING
        job.lease_until = None
    media_env["transcription"].script = [
        TranscriptionResult(
            text="повторная оплата", model="gpt-transcribe", latency_ms=1
        )
    ]

    await media_env["processor"].run_once()

    assert len(media_env["transcription"].requests) == 1  # still ONE paid call
    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "already_charged"
    async with maker() as db:
        calls = (await db.execute(select(AiCall))).scalars().all()
    assert len(calls) == 1


async def test_no_second_claim_while_a_lease_is_held(media_env) -> None:
    """Two overlapping passes must not download or pay twice."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    await add_media_message(maker, chat_id)

    processor = media_env["processor"]
    claimed_first = await processor._claim_batch()
    claimed_second = await processor._claim_batch()

    assert len(claimed_first) == 1
    assert claimed_second == []  # the live lease hides the job


async def test_failed_transcription_releases_the_reservation_and_retries(
    media_env,
) -> None:
    maker = media_env["maker"]
    media_env["transcription"].script = [
        ChatError("transcription_failed:503", retryable=True)
    ]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()

    job = await get_job(maker, message_id)
    assert job.state is MediaJobState.PENDING  # will be retried
    assert job.next_attempt_at is not None
    assert (await get_message(maker, message_id)).state is MsgState.QUEUED
    async with maker() as db:
        day = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()
        call = (await db.execute(select(AiCall))).scalars().one()
    # Nothing was spent, and the estimate is not stuck as reserved.
    assert day.spent_usd == 0
    assert day.reserved_usd == 0
    assert call.status == "error"


async def test_retries_are_bounded_and_end_with_a_human(media_env) -> None:
    maker = media_env["maker"]
    media_env["download"].error = ProviderError(
        "downloadFile", status_code=None, retryable=True
    )
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    for _ in range(5):
        async with maker() as db, db.begin():
            job = (
                await db.execute(
                    select(MediaJob).where(MediaJob.message_id == message_id)
                )
            ).scalar_one()
            job.next_attempt_at = None  # skip the backoff wait
            job.lease_until = None
        await media_env["processor"].run_once()

    job = await get_job(maker, message_id)
    assert job.state is MediaJobState.FAILED
    assert job.attempts <= 4  # retry_max_attempts, not an endless loop
    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert [r.body for r in await get_replies(maker, chat_id)] == [
        DEFAULT_TEMPLATES["media"]["ru"]
    ]


async def test_empty_transcript_is_terminal_not_an_infinite_loop(media_env) -> None:
    """We paid and got nothing (silence). Paying again would change
    nothing, so the message becomes a human's job."""
    maker = media_env["maker"]
    media_env["transcription"].script = [
        TranscriptionResult(text="   ", model="gpt-transcribe", latency_ms=10)
    ]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)

    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.state is MsgState.HANDED_TO_HUMAN
    assert message.error_code == "empty_transcript"
    job = await get_job(maker, message_id)
    assert job.state is MediaJobState.FAILED
    assert await media_env["processor"].run_once() == 0  # nothing left to claim


async def test_operator_media_is_never_processed(media_env) -> None:
    """Invariant 7: only a customer's incoming message reaches AI. A
    manager's own voice note must not be transcribed at our expense."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    async with maker() as db, db.begin():
        message = Message(
            chat_id=chat_id,
            direction=MsgDirection.OUT,
            source=MsgSource.OPERATOR_PHONE,
            provider_message_id="pm-operator-1",
            media_type=MediaType.VOICE,
            state=MsgState.SENT,
        )
        db.add(message)
        await db.flush()
        # Even if a job somehow exists for it:
        await enqueue_media_job(
            db,
            message_id=message.id,
            chat_id=chat_id,
            media_type=MediaType.VOICE,
            download_url=DOWNLOAD_URL,
            mime_type="audio/ogg",
        )

    assert await media_env["processor"].run_once() == 0
    assert media_env["transcription"].requests == []


async def test_enqueue_is_idempotent_per_message(media_env) -> None:
    """A redelivered notification cannot create a second paid unit."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(maker, chat_id)
    async with maker() as db, db.begin():
        await enqueue_media_job(
            db,
            message_id=message_id,
            chat_id=chat_id,
            media_type=MediaType.VOICE,
            download_url=DOWNLOAD_URL,
            mime_type="audio/ogg",
        )
    async with maker() as db:
        jobs = (
            (
                await db.execute(
                    select(MediaJob).where(MediaJob.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1


async def test_documents_get_no_job_at_all(media_env) -> None:
    """Documents are never read: no job, no download, no OpenAI. The
    acknowledgement template comes from the AI pipeline instead."""
    maker = media_env["maker"]
    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(
        maker, chat_id, media_type=MediaType.DOCUMENT, mime_type="application/pdf"
    )

    async with maker() as db:
        jobs = (
            (
                await db.execute(
                    select(MediaJob).where(MediaJob.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert jobs == []
    assert await media_env["processor"].run_once() == 0


async def test_image_is_stored_re_encoded_without_exif(media_env) -> None:
    """What lands on disk is OUR jpeg: EXIF (GPS, device) is gone before
    the file is written, and an "image" that was really SVG/HTML can
    never reach the admin's browser."""
    maker = media_env["maker"]
    source = io.BytesIO()
    image = Image.new("RGB", (2000, 1500), color=(10, 20, 30))
    exif = image.getexif()
    exif[271] = "TestCamera"
    image.save(source, format="JPEG", exif=exif)
    media_env["download"].payload = source.getvalue()

    chat_id = await make_chat(maker, media_env["instance_pk"])
    message_id = await add_media_message(
        maker, chat_id, media_type=MediaType.IMAGE, mime_type="image/jpeg"
    )
    await media_env["processor"].run_once()

    message = await get_message(maker, message_id)
    assert message.transcript == DESCRIPTION
    stored = storage.read_bytes(media_env["media_dir"], message.media_ref)
    with Image.open(io.BytesIO(stored)) as result:
        assert dict(result.getexif()) == {}
        assert max(result.size) == 1280  # shrunk for money
