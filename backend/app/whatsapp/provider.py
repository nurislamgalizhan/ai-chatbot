"""Transport abstraction: business logic never talks to GREEN-API directly.

A second provider (e.g. Meta WABA) implements the same interface without
touching chats, RAG or the admin panel (SPEC §15).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class ProviderError(Exception):
    """Transport failure, sanitized: never contains URLs or tokens."""

    def __init__(self, method: str, *, status_code: int | None, retryable: bool) -> None:
        self.method = method
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"{method} failed (status={status_code}, retryable={retryable})")


class UnsupportedOperationError(ProviderError):
    """The provider does not implement this optional capability."""

    def __init__(self, method: str) -> None:
        super().__init__(method, status_code=None, retryable=False)


@dataclass(frozen=True)
class RawNotification:
    """One notification exactly as delivered by the provider queue."""

    receipt_id: int
    body: dict[str, Any]


@dataclass(frozen=True)
class SendResult:
    # None = the provider accepted the send but did not return an id.
    provider_message_id: str | None


class WhatsAppProvider(ABC):
    """Minimal transport interface (SPEC §15)."""

    #: Whether set_typing() is available. GREEN-API does not expose typing.
    supports_typing: bool = False

    @abstractmethod
    async def poll_notification(self) -> RawNotification | None:
        """Long-poll one notification; None when the queue is empty."""

    @abstractmethod
    async def delete_notification(self, receipt_id: int) -> None:
        """Acknowledge a notification. Called ONLY after our commit."""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str) -> SendResult: ...

    @abstractmethod
    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult: ...

    async def set_typing(self, chat_id: str) -> None:
        raise UnsupportedOperationError("set_typing")

    @abstractmethod
    async def get_instance_state(self) -> str:
        """Provider-reported instance state, e.g. "authorized"."""

    @abstractmethod
    async def aclose(self) -> None: ...
