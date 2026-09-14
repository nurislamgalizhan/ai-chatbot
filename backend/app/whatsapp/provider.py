"""Transport abstraction: business logic never talks to GREEN-API directly.

A second provider (e.g. Meta WABA) implements the same interface without
touching chats, RAG or the admin panel (SPEC §15).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class InstanceHealth:
    """What the provider says about itself, beyond "authorized".

    Every field is optional: a provider that cannot report something must
    leave it None rather than let the status page invent a value.
    """

    #: Socket state ("online"/"offline" for GREEN-API): a number can be
    #: authorized while the socket is closed, and then nothing flows.
    socket: str | None = None
    #: Notifications waiting on the PROVIDER's side. Growing = we are not
    #: collecting them (or they are being delivered somewhere else).
    incoming_queue: int | None = None
    #: Our own sends the provider has not delivered yet.
    outgoing_queue: int | None = None
    #: Instance settings that decide whether this bot can work at all
    #: (see `settings_problems`).
    settings: dict[str, Any] = field(default_factory=dict)
    #: Human-readable reasons the instance configuration breaks the bot.
    settings_problems: list[str] = field(default_factory=list)


class WhatsAppProvider(ABC):
    """Minimal transport interface (SPEC §15)."""

    #: Whether a typing indicator can be requested. For GREEN-API it is
    #: free: `sendMessage` takes a `typingTime`, so no extra request.
    supports_typing: bool = False

    @abstractmethod
    async def poll_notification(self) -> RawNotification | None:
        """Long-poll one notification; None when the queue is empty."""

    @abstractmethod
    async def delete_notification(self, receipt_id: int) -> None:
        """Acknowledge a notification. Called ONLY after our commit."""

    @abstractmethod
    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
        typing_time_ms: int | None = None,
        link_preview: bool = True,
    ) -> SendResult:
        """Send a text message.

        The keyword options are all optional capabilities: a provider that
        lacks them ignores them rather than failing, because none of them
        changes whether the customer gets an answer.
        """

    @abstractmethod
    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult: ...

    async def set_typing(self, chat_id: str) -> None:
        raise UnsupportedOperationError("set_typing")

    async def download_file(self, url: str, max_bytes: int) -> bytes:
        """Fetch incoming media, refusing anything over max_bytes.

        The cap must be enforced WHILE streaming, not after: a provider
        (or anything impersonating one) could otherwise stream gigabytes
        into a 3.8 GiB VPS. Optional capability — a provider without it
        makes the pipeline hand media to a human.
        """
        raise UnsupportedOperationError("download_file")

    @abstractmethod
    async def get_instance_state(self) -> str:
        """Provider-reported instance state, e.g. "authorized"."""

    async def get_health(self) -> InstanceHealth:
        """Everything the provider can say about its own readiness.

        Optional: a provider without these endpoints returns an empty
        health object, and the status page shows "не сообщается" instead
        of a made-up number.
        """
        return InstanceHealth()

    async def check_number(self, phone: str) -> bool | None:
        """Does this number have WhatsApp? None = cannot tell."""
        return None

    @abstractmethod
    async def aclose(self) -> None: ...
