"""Chat model providers behind one interface.

Tests and the sandbox-without-a-key path inject FakeChatProvider; the
real OpenAI provider is constructed only when a key is configured. The
model NEVER gets tools (SPEC invariant: no data/file/network access).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)

# HTTP statuses worth retrying: rate limit and server-side failures.
_RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


class ChatError(Exception):
    """Provider failure; `retryable` drives the pipeline's backoff."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ChatUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class ChatRequest:
    """system + alternating history; content only, no tools ever."""

    system: str
    messages: list[dict[str, str]]  # {"role": "user"|"assistant", "content": ...}
    model: str
    max_output_tokens: int
    reasoning_effort: str


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: ChatUsage
    model: str
    latency_ms: int


class ChatProvider(ABC):
    @abstractmethod
    async def complete(self, request: ChatRequest) -> ChatResult: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return


class OpenAIChatProvider(ChatProvider):
    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            msg = "openai_api_key_missing"
            raise ChatError(msg, retryable=False)
        # Lazy import: processes that never chat skip the SDK import cost.
        from openai import AsyncOpenAI  # noqa: PLC0415

        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=settings.openai_request_timeout
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        import openai  # noqa: PLC0415

        started = time.monotonic()
        try:
            response = await self._client.chat.completions.create(  # type: ignore[call-overload]
                model=request.model,
                messages=[
                    {"role": "system", "content": request.system},
                    *request.messages,
                ],
                max_completion_tokens=request.max_output_tokens,
                reasoning_effort=request.reasoning_effort,
            )
        except openai.APIStatusError as exc:
            retryable = exc.status_code in _RETRYABLE_STATUSES
            logger.error("chat_request_failed", status=exc.status_code, retryable=retryable)
            msg = f"chat_failed:http_{exc.status_code}"
            raise ChatError(msg, retryable=retryable) from exc
        except openai.APIConnectionError as exc:  # includes timeouts
            logger.error("chat_request_failed", error=type(exc).__name__, retryable=True)
            msg = f"chat_failed:{type(exc).__name__}"
            raise ChatError(msg, retryable=True) from exc
        except Exception as exc:
            logger.error("chat_request_failed", error=type(exc).__name__, retryable=False)
            msg = f"chat_failed:{type(exc).__name__}"
            raise ChatError(msg, retryable=False) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice else ""
        if not text.strip():
            msg = "chat_failed:empty_response"
            raise ChatError(msg, retryable=True)
        usage = response.usage
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        return ChatResult(
            text=text,
            usage=ChatUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                cached_input_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
            ),
            model=response.model or request.model,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.close()


@dataclass
class FakeChatProvider(ChatProvider):
    """Scripted responses for tests: records every request, replays the
    given results in order (or raises the given errors)."""

    script: list[ChatResult | ChatError] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)

    async def complete(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if not self.script:
            msg = "chat_failed:fake_script_exhausted"
            raise ChatError(msg, retryable=False)
        item = self.script.pop(0)
        if isinstance(item, ChatError):
            raise item
        return item


def make_result(text: str, *, input_tokens: int = 100, output_tokens: int = 50) -> ChatResult:
    """Test helper: a plausible ChatResult without ceremony."""
    return ChatResult(
        text=text,
        usage=ChatUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-chat-model",
        latency_ms=5,
    )
