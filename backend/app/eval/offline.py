"""Stand-ins for the paid providers, used by the offline eval and the
load test.

They are deliberately simple and deliberately NOT smart:

* `LexicalEmbeddings` maps a text to a vector built from its word
  prefixes, so two texts sharing content words are similar and unrelated
  texts are not. It knows nothing about meaning or other languages — a
  Kazakh question does not match a Russian document, which is why the
  offline eval measures routing, not answer quality, and says so.
* `OracleChatProvider` "answers" by echoing the numbers found in the
  retrieved chunks, in the language the pipeline asked for. That makes
  two things checkable without OpenAI: that the right document reached
  the prompt, and that the language directive reached the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.ai.chat_provider import ChatProvider, ChatRequest, ChatResult, ChatUsage, VisionRequest
from app.ai.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider, EmbeddingResult
from app.ai.tokens import count_tokens
from app.ai.transcription import TranscriptionProvider, TranscriptionRequest, TranscriptionResult
from app.services.kb_text import token_key
from app.whatsapp.provider import ProviderError, SendResult, WhatsAppProvider

if TYPE_CHECKING:
    from app.whatsapp.provider import RawNotification

#: Relevance threshold for the lexical stand-in. The production value
#: (0.35) belongs to the real embedding space; here two shared content
#: words between a short question and a chunk score around 0.1–0.2.
OFFLINE_RAG_MIN_SCORE = 0.10

_SHORT_PREFIX = 3
_LONG_PREFIX = 4
#: A real model reads every retrieved chunk; so does the stand-in — up
#: to this many numbers per chunk and in total, in order of appearance.
_MAX_NUMBERS_PER_CHUNK = 12
_MAX_NUMBERS_IN_ANSWER = 30

_LANGUAGE_LINE_RE = re.compile(r"Язык клиента: (ru|kk|en)")
_SERVICE_LANGUAGE_RE = re.compile(r"Язык ответа: (ru|kk|en)")
_CHUNK_RE = re.compile(r"<kb_chunk[^>]*>(.*?)</kb_chunk>", re.DOTALL)
# Prices (2000, 26 000), times (07:00) and phone numbers as they appear
# in the knowledge base. Order of appearance is kept.
_NUMBER_RE = re.compile(r"\+?\d[\d ]{1,14}\d|\d{1,2}:\d{2}|\d{2,6}")

_ANSWER = {
    "ru": (
        "По данным центра: {numbers}. Подсказать что-то ещё?",
        "Могу уточнить детали — что именно вас интересует?",
    ),
    "kk": (
        "Орталық деректері бойынша: {numbers}. Тағы бір нәрсе айтайын ба?",
        "Мәліметтерді нақтылай аламын — сізді не қызықтырады?",
    ),
    "en": (
        "According to the centre's data: {numbers}. Anything else?",
        "I can clarify the details — what exactly are you interested in?",
    ),
}
_SERVICE = {
    "ru": "Передаю ваш вопрос администратору — с вами свяжутся.",
    "kk": "Сұрағыңызды әкімшіге жеткіземін — сізбен хабарласады.",
    "en": "I am passing your question to an administrator — they will contact you.",
}


def lexical_tokens(text: str) -> list[str]:
    """Word prefixes of the significant words: `абонемент` -> `або`, `абон`.

    Two prefixes per word so that «зал» meets «залов» and «стоит» meets
    «стоимость»; one- and two-letter words are noise and are dropped.
    """
    tokens: set[str] = set()
    for word in token_key(text):
        if len(word) < _SHORT_PREFIX:
            continue
        tokens.add(word[:_SHORT_PREFIX])
        if len(word) >= _LONG_PREFIX:
            tokens.add(word[:_LONG_PREFIX])
    return sorted(tokens)


class LexicalEmbeddings(EmbeddingProvider):
    """Hashing-trick bag of word prefixes, unit-normalised.

    `model` defaults to a name that has no price so nothing is booked
    for it; the load test passes the configured embedding model name so
    the scratch database's ai_calls look like the real thing.
    """

    def __init__(self, *, model: str = "lexical-offline") -> None:
        self.model = model
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(
            vectors=[self.vector(text) for text in texts],
            model=self.model,
            total_tokens=sum(max(1, count_tokens(text)) for text in texts),
        )

    @staticmethod
    def vector(text: str) -> list[float]:
        tokens = lexical_tokens(text) or ["<empty>"]
        raw = [0.0] * EMBEDDING_DIMENSIONS
        for token in tokens:
            digest = hashlib.sha1(token.encode("utf-8"), usedforsecurity=False).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            raw[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


def _numbers_in_chunks(messages: list[dict[str, str]]) -> list[str]:
    seen: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        for chunk in _CHUNK_RE.findall(message["content"]):
            taken = 0
            for match in _NUMBER_RE.findall(chunk):
                value = match.strip()
                digits = sum(ch.isdigit() for ch in value)
                if digits < 2 or value in seen:  # noqa: PLR2004 - "3 месяца" is noise
                    continue
                seen.append(value)
                taken += 1
                if taken >= _MAX_NUMBERS_PER_CHUNK:
                    break
    return seen


@dataclass
class OracleChatProvider(ChatProvider):
    """Deterministic model stand-in that records everything it is asked.

    `latency_seconds` lets the load test model a slow model; `in_flight`
    / `max_in_flight` show how many calls ran concurrently, which is the
    number SPEC §16 caps at 8.
    """

    latency_seconds: float = 0.0
    requests: list[ChatRequest] = field(default_factory=list)
    vision_requests: list[VisionRequest] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency_seconds:
                await asyncio.sleep(self.latency_seconds)
            text = self.answer(request)
        finally:
            self.in_flight -= 1
        usage = ChatUsage(
            input_tokens=count_tokens(request.system)
            + sum(count_tokens(m["content"]) for m in request.messages),
            output_tokens=max(1, count_tokens(text)),
        )
        return ChatResult(
            text=text,
            usage=usage,
            model=request.model,
            latency_ms=int(self.latency_seconds * 1000),
        )

    async def describe_image(self, request: VisionRequest) -> ChatResult:
        self.vision_requests.append(request)
        return ChatResult(
            text="На фото: стойка администратора и прайс-лист.",
            usage=ChatUsage(input_tokens=1200, output_tokens=20),
            model=request.model,
            latency_ms=int(self.latency_seconds * 1000),
        )

    @staticmethod
    def answer(request: ChatRequest) -> str:
        last = request.messages[-1]["content"] if request.messages else ""
        service = _SERVICE_LANGUAGE_RE.search(last)
        if last.startswith("Ситуация:") and service:
            return _SERVICE[service.group(1)]
        language = "ru"
        for message in request.messages:
            found = _LANGUAGE_LINE_RE.search(message["content"])
            if found:
                language = found.group(1)
                break
        with_numbers, without = _ANSWER[language]
        numbers = _numbers_in_chunks(request.messages)[:_MAX_NUMBERS_IN_ANSWER]
        if numbers:
            return with_numbers.format(numbers=", ".join(numbers))
        return without


@dataclass
class RecordingChatProvider(ChatProvider):
    """Wraps a real provider so the eval can see what was sent — both
    modes then share one observation code path."""

    inner: ChatProvider
    requests: list[ChatRequest] = field(default_factory=list)

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return await self.inner.complete(request)

    async def describe_image(self, request: VisionRequest) -> ChatResult:
        return await self.inner.describe_image(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class StaticTranscription(TranscriptionProvider):
    """Every voice note becomes the same text: the load test is about the
    queue, not about speech recognition."""

    text: str
    model: str = "offline-transcribe"
    requests: list[TranscriptionRequest] = field(default_factory=list)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        return TranscriptionResult(text=self.text, model=self.model, latency_ms=50)


class FileServingProvider(WhatsAppProvider):
    """WhatsApp transport for the load test: serves one audio payload for
    any download URL and refuses to send anything (DRY_RUN never asks)."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.downloads = 0

    async def poll_notification(self) -> RawNotification | None:
        return None

    async def delete_notification(self, receipt_id: int) -> None:
        return None

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
        typing_time_ms: int | None = None,
        link_preview: bool = True,
    ) -> SendResult:
        raise ProviderError("sendMessage", status_code=None, retryable=False)

    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult:
        raise ProviderError("sendFileByUpload", status_code=None, retryable=False)

    async def download_file(self, url: str, max_bytes: int) -> bytes:
        self.downloads += 1
        if len(self.payload) > max_bytes:
            raise ProviderError("downloadFile.too_large", status_code=None, retryable=False)
        return self.payload

    async def get_instance_state(self) -> str:
        return "authorized"

    async def aclose(self) -> None:
        return None
