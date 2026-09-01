"""Embedding providers behind one interface.

Tests inject FakeEmbeddingProvider — the real OpenAI API is never called
from the test suite. The worker builds the OpenAI provider only when a
key is configured; without a key indexing reports an honest error
instead of silently producing garbage vectors.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)

EMBEDDING_DIMENSIONS = 1536


class EmbeddingError(Exception):
    """Provider failure; the caller marks the document as failed."""


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    total_tokens: int


class EmbeddingProvider(ABC):
    model: str

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingResult: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            msg = "openai_api_key_missing"
            raise EmbeddingError(msg)
        # Imported lazily: the API process never embeds, so it should not
        # pay the SDK import cost at startup.
        from openai import AsyncOpenAI  # noqa: PLC0415

        self.model = settings.openai_embedding_model
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=settings.openai_request_timeout
        )

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        try:
            response = await self._client.embeddings.create(model=self.model, input=texts)
        except Exception as exc:  # SDK raises a family of errors
            logger.error("embedding_request_failed", error=type(exc).__name__)
            msg = f"embedding_failed:{type(exc).__name__}"
            raise EmbeddingError(msg) from exc
        vectors = [item.embedding for item in response.data]
        if len(vectors) != len(texts):
            msg = "embedding_count_mismatch"
            raise EmbeddingError(msg)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            total_tokens=getattr(response.usage, "total_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._client.close()


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic unit vectors derived from the text hash.

    Same text -> same vector, similar texts -> unrelated vectors. Good
    enough to test the plumbing (storage, filters, thresholds); it does
    NOT emulate semantic similarity, so relevance itself is asserted via
    explicitly seeded vectors in the tests that care.
    """

    model = "fake-embedding-model"

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        return EmbeddingResult(
            vectors=[self._vector(text) for text in texts],
            model=self.model,
            total_tokens=sum(len(text) for text in texts) // 4,
        )

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [digest[i % len(digest)] / 255.0 - 0.5 for i in range(self._dimensions)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]
