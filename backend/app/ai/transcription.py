"""Voice transcription behind one interface (SPEC §13).

Billing is per MINUTE of audio, not per token, so the duration is
measured locally before the call (app/media/audio.py) and the reserved
amount is based on it. The audio file itself never reaches the chat
model — only the resulting text does.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import structlog

from app.ai.chat_provider import ChatError

logger = structlog.get_logger(__name__)

_RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class TranscriptionRequest:
    audio: bytes
    file_name: str
    model: str
    # Domain terms (org name, section names, street) so the model spells
    # them correctly. Owner-supplied setting — no company names in code.
    terms: str = ""
    language: str | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    model: str
    latency_ms: int
    # Provider-reported duration when available; the caller falls back to
    # its own local measurement for billing.
    duration_seconds: float | None = None


class TranscriptionProvider(ABC):
    @abstractmethod
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return


class OpenAITranscriptionProvider(TranscriptionProvider):
    def __init__(self, settings: object) -> None:
        api_key = getattr(settings, "openai_api_key", "")
        if not api_key:
            msg = "openai_api_key_missing"
            raise ChatError(msg, retryable=False)
        from openai import AsyncOpenAI  # noqa: PLC0415 - lazy: only the worker transcribes

        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=getattr(settings, "openai_request_timeout", 60.0),
        )

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        import openai  # noqa: PLC0415

        started = time.monotonic()
        try:
            response = await self._client.audio.transcriptions.create(
                model=request.model,
                file=(request.file_name, request.audio),
                # `omit` is the SDK's "parameter absent" sentinel: sending
                # an empty prompt/language would not be the same thing.
                prompt=request.terms or openai.omit,
                language=request.language or openai.omit,
            )
        except openai.APIStatusError as exc:
            retryable = exc.status_code in _RETRYABLE_STATUSES
            logger.error("transcription_failed", status=exc.status_code, retryable=retryable)
            msg = f"transcription_failed:http_{exc.status_code}"
            raise ChatError(msg, retryable=retryable) from exc
        except openai.APIConnectionError as exc:
            logger.error("transcription_failed", error=type(exc).__name__, retryable=True)
            msg = f"transcription_failed:{type(exc).__name__}"
            raise ChatError(msg, retryable=True) from exc
        except Exception as exc:
            logger.error("transcription_failed", error=type(exc).__name__, retryable=False)
            msg = f"transcription_failed:{type(exc).__name__}"
            raise ChatError(msg, retryable=False) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        text = (getattr(response, "text", "") or "").strip()
        duration = getattr(response, "duration", None)
        return TranscriptionResult(
            text=text,
            model=request.model,
            latency_ms=latency_ms,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        )

    async def aclose(self) -> None:
        await self._client.close()


@dataclass
class FakeTranscriptionProvider(TranscriptionProvider):
    """Scripted transcripts for tests; records every request so a test
    can assert that a paid call did NOT happen."""

    script: list[TranscriptionResult | ChatError] = field(default_factory=list)
    requests: list[TranscriptionRequest] = field(default_factory=list)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        if not self.script:
            msg = "transcription_failed:fake_script_exhausted"
            raise ChatError(msg, retryable=False)
        item = self.script.pop(0)
        if isinstance(item, ChatError):
            raise item
        return item
