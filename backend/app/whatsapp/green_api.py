"""GREEN-API implementation of WhatsAppProvider.

Security note: GREEN-API puts the token into the URL path. Every error is
re-raised as a sanitized ProviderError that carries neither URL nor token,
and httpx logging stays disabled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from httpx._client import UseClientDefault
from pydantic import ValidationError

from app.whatsapp.download_guard import DownloadNotAllowedError, check_download_url
from app.whatsapp.provider import (
    InstanceHealth,
    ProviderError,
    RawNotification,
    SendResult,
    WhatsAppProvider,
)
from app.whatsapp.schemas import (
    InstanceStateResponse,
    NotificationEnvelope,
    SendMessageResponse,
)

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_HTTP_ERROR_MIN = 400
#: WhatsApp accepts 1..20 seconds of "typing"; anything outside is an error.
_TYPING_MIN_MS = 1000
_TYPING_MAX_MS = 20000

#: Instance settings that decide whether this bot works at all. Everything
#: else the provider returns is none of our business (and some of it is
#: sensitive, e.g. the webhook token).
_WATCHED_SETTINGS = frozenset(
    {
        "webhookUrl",
        "incomingWebhook",
        "outgoingWebhook",
        "outgoingAPIMessageWebhook",
        "stateWebhook",
        "markIncomingMessagesReaded",
        "delaySendMessagesMilliseconds",
        "keepOnlineStatus",
    }
)


def settings_problems(settings: dict[str, Any]) -> list[str]:
    """Owner-facing reasons this instance configuration breaks the bot.

    This is the check that catches the worst failure mode in the whole
    system: with a webhook URL configured, the provider delivers
    notifications THERE and our polling queue stays empty forever. No
    error, no exception — the bot is simply silent for every customer,
    and «последний приём сообщений» keeps ticking because polling itself
    succeeds (it just returns nothing).
    """
    if not settings:
        return []
    problems: list[str] = []
    if str(settings.get("webhookUrl") or "").strip():
        problems.append(
            "На инстансе задан адрес вебхука. Уведомления уходят туда, а бот их "
            "забирает опросом — он не получит ни одного сообщения. Поле webhookUrl "
            "в панели GREEN-API должно быть пустым."
        )
    if str(settings.get("incomingWebhook", "yes")).lower() == "no":
        problems.append(
            "В настройках инстанса выключены уведомления о входящих сообщениях "
            "(incomingWebhook = no) — бот не увидит вопросы клиентов."
        )
    if str(settings.get("outgoingWebhook", "yes")).lower() == "no":
        problems.append(
            "Выключены уведомления об исходящих сообщениях (outgoingWebhook = no). "
            "Бот не узнает, что менеджер ответил клиенту с телефона, и продолжит "
            "отвечать сам."
        )
    if str(settings.get("outgoingAPIMessageWebhook", "yes")).lower() == "no":
        problems.append(
            "Выключены уведомления о сообщениях, отправленных ботом "
            "(outgoingAPIMessageWebhook = no) — пропадут отметки доставки."
        )
    return problems


def _classify_status(method: str, status_code: int) -> ProviderError:
    return ProviderError(
        method, status_code=status_code, retryable=status_code in _RETRYABLE_STATUS
    )


class GreenApiProvider(WhatsAppProvider):
    # Typing is free here: sendMessage carries a typingTime field, so the
    # indicator costs no extra request (SPEC §16 allowed it on exactly
    # that condition). A separate sendTyping method also exists; we do
    # not use it precisely because it WOULD be an extra request.
    supports_typing = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.green_api_id_instance or not settings.green_api_token:
            msg = "green_api_configuration_missing"
            raise ProviderError(msg, status_code=None, retryable=False)
        self._instance_id = settings.green_api_id_instance
        self._token = settings.green_api_token
        self._receive_timeout = settings.green_api_receive_timeout
        self._client = client or httpx.AsyncClient(
            base_url=settings.green_api_base_url,
            timeout=settings.green_api_request_timeout,
        )
        self._media_client = httpx.AsyncClient(
            base_url=settings.green_api_media_url,
            timeout=max(settings.green_api_request_timeout, 60.0),
        )
        # Incoming media: the payload carries an ABSOLUTE https URL, so
        # this client has no base_url. Redirects are not followed — a
        # redirect from an untrusted payload is an SSRF primitive.
        self._download_client = httpx.AsyncClient(
            timeout=settings.media_download_timeout,
            follow_redirects=False,
        )
        self._download_hosts = settings.media_download_host_list

    def _path(self, method: str) -> str:
        return f"/waInstance{self._instance_id}/{method}/{self._token}"

    async def _request(
        self,
        method_name: str,
        http_method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        # NOT `None`: in httpx, timeout=None means "wait forever". Every
        # caller that omitted a timeout was silently asking for exactly
        # that — and a send that hangs forever holds the outbox
        # transaction open, so PostgreSQL eventually kills the session
        # AFTER the customer already received the message, and the next
        # pass sends it again.
        timeout: float | httpx.Timeout | UseClientDefault = httpx.USE_CLIENT_DEFAULT,
    ) -> Any:  # noqa: ANN401 - provider payloads are schemaless JSON
        try:
            response = await self._client.request(
                http_method, path, json=json, params=params, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                method_name, status_code=None, retryable=True
            ) from exc  # network/timeout: retryable, no URL in the message
        if response.status_code >= _HTTP_ERROR_MIN:
            raise _classify_status(method_name, response.status_code)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            # 200 with a body that is not JSON: a proxy error page, a
            # captcha, a truncated response. Not an httpx.HTTPError, so
            # without this it escaped as a bare ValueError and the outbox
            # killed the message as a poison row — the customer never got
            # an answer that was merely delayed. Retryable transport.
            raise ProviderError(method_name, status_code=None, retryable=True) from exc

    async def poll_notification(self) -> RawNotification | None:
        payload = await self._request(
            "receiveNotification",
            "GET",
            self._path("receiveNotification"),
            params={"receiveTimeout": self._receive_timeout},
            # long-poll: the server holds the request up to receive_timeout
            timeout=self._receive_timeout + 15.0,
        )
        if payload is None:
            return None
        try:
            envelope = NotificationEnvelope.model_validate(payload)
        except ValidationError as exc:
            # No usable receiptId means the notification cannot be acked at
            # all — a protocol-level provider failure; retry is the only
            # option and the loop will back off. (The lenient body coercion
            # in the schema makes this reachable only when receiptId itself
            # is missing or non-numeric.)
            logger.error("receive_envelope_invalid", errors=exc.error_count())
            raise ProviderError(
                "receiveNotification.envelope", status_code=None, retryable=True
            ) from exc
        return RawNotification(receipt_id=envelope.receipt_id, body=envelope.body)

    async def delete_notification(self, receipt_id: int) -> None:
        await self._request(
            "deleteNotification",
            "DELETE",
            self._path(f"deleteNotification/{receipt_id}"),
        )

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
        typing_time_ms: int | None = None,
        link_preview: bool = True,
    ) -> SendResult:
        body: dict[str, Any] = {"chatId": chat_id, "message": text}
        if quoted_message_id:
            # Quoting the customer's own question: in a chat where a
            # manager also writes from the phone, an unquoted answer can
            # arrive under someone else's line and read as an answer to it.
            body["quotedMessageId"] = quoted_message_id
        if typing_time_ms:
            # The typing indicator costs NO extra request — it is a field
            # of sendMessage itself (1000..20000 ms). It does delay the
            # delivery by that long, which is why it is a setting.
            body["typingTime"] = max(_TYPING_MIN_MS, min(int(typing_time_ms), _TYPING_MAX_MS))
        if not link_preview:
            # Default on the provider side is true: a link in a short
            # answer then pulls up to 2 MB of preview.
            body["linkPreview"] = False
        payload = await self._request(
            "sendMessage",
            "POST",
            self._path("sendMessage"),
            json=body,
        )
        return self._send_result("sendMessage", payload)

    async def send_file(
        self, chat_id: str, file_path: str, file_name: str, caption: str | None = None
    ) -> SendResult:
        data: dict[str, Any] = {"chatId": chat_id, "fileName": file_name}
        if caption:
            data["caption"] = caption
        try:
            with Path(file_path).open("rb") as file_obj:
                response = await self._media_client.post(
                    self._path("sendFileByUpload"),
                    data=data,
                    files={"file": (file_name, file_obj)},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("sendFileByUpload", status_code=None, retryable=True) from exc
        if response.status_code >= _HTTP_ERROR_MIN:
            raise _classify_status("sendFileByUpload", response.status_code)
        return self._send_result("sendFileByUpload", response.json())

    async def download_file(self, url: str, max_bytes: int) -> bytes:
        """Stream an incoming media file, aborting past max_bytes.

        The URL comes from an untrusted payload, so it is checked against
        the SSRF guard (https, host allowlist, public address) before a
        single byte is fetched, redirects are not followed, and nothing
        about the URL is ever logged or put into the error.
        """
        try:
            # getaddrinfo is blocking: a slow resolver must not stall the
            # polling loop, the outbox and the heartbeat with it.
            await asyncio.to_thread(check_download_url, url, self._download_hosts)
        except DownloadNotAllowedError as exc:
            raise ProviderError(str(exc), status_code=None, retryable=False) from exc
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._download_client.stream("GET", url) as response:
                if response.status_code >= _HTTP_ERROR_MIN:
                    raise _classify_status("downloadFile", response.status_code)
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise ProviderError("downloadFile.too_large", status_code=None, retryable=False)
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        # Abort mid-stream: a lying content-length must not
                        # be able to fill the disk.
                        raise ProviderError(
                            "downloadFile.too_large", status_code=None, retryable=False
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ProviderError("downloadFile", status_code=None, retryable=True) from exc
        return b"".join(chunks)

    async def get_health(self) -> InstanceHealth:
        """Socket state, both provider-side queues and the settings that
        decide whether this bot can receive anything at all.

        Four independent calls, made only when the owner opens «Система»
        or on the slow monitoring pass — never on a customer's path.
        """
        socket = await self._socket_status()
        settings = await self._instance_settings()
        return InstanceHealth(
            socket=socket,
            incoming_queue=await self._incoming_queue(),
            outgoing_queue=await self._outgoing_queue(),
            settings=settings,
            settings_problems=settings_problems(settings),
        )

    async def _socket_status(self) -> str | None:
        try:
            payload = await self._request(
                "getStatusInstance", "GET", self._path("getStatusInstance")
            )
        except ProviderError:
            return None
        if isinstance(payload, dict):
            value = payload.get("statusInstance")
            return str(value) if value is not None else None
        return None

    async def _instance_settings(self) -> dict[str, Any]:
        try:
            payload = await self._request("getSettings", "GET", self._path("getSettings"))
        except ProviderError:
            return {}
        return (
            {k: v for k, v in payload.items() if k in _WATCHED_SETTINGS}
            if isinstance(payload, dict)
            else {}
        )

    async def _incoming_queue(self) -> int | None:
        try:
            payload = await self._request("getWebhooksCount", "GET", self._path("getWebhooksCount"))
        except ProviderError:
            return None
        if isinstance(payload, dict):
            count = payload.get("count")
            return int(count) if isinstance(count, int | float) else None
        return None

    async def _outgoing_queue(self) -> int | None:
        """Our sends the provider has NOT delivered yet.

        This is the one number that reveals the worst silent failure: the
        API accepted the message (we recorded it as sent), the instance is
        not authorized, and it sits in the provider's queue for 24 hours
        while the customer waits.
        """
        try:
            payload = await self._request(
                "showMessagesQueue", "GET", self._path("showMessagesQueue")
            )
        except ProviderError:
            return None
        return len(payload) if isinstance(payload, list) else None

    async def check_number(self, phone: str) -> bool | None:
        """Does this number have WhatsApp? Used before trusting a number
        the owner typed by hand (their own alert number)."""
        digits = phone.lstrip("+")
        if not digits.isdigit():
            return None
        try:
            payload = await self._request(
                "checkWhatsapp",
                "POST",
                self._path("checkWhatsapp"),
                json={"phoneNumber": int(digits)},
            )
        except ProviderError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("existsWhatsapp"), bool):
            return bool(payload["existsWhatsapp"])
        return None

    async def get_instance_state(self) -> str:
        payload = await self._request("getStateInstance", "GET", self._path("getStateInstance"))
        try:
            return InstanceStateResponse.model_validate(payload or {}).state_instance
        except ValidationError:
            return "unknown"

    @staticmethod
    def _send_result(method: str, payload: object) -> SendResult:
        """HTTP 2xx means the provider ACCEPTED the message. A missing
        idMessage must not fail the send: failing would schedule a retry
        and deliver the message to the customer twice."""
        try:
            parsed = SendMessageResponse.model_validate(payload or {})
        except ValidationError:
            parsed = SendMessageResponse()
        if parsed.id_message is None:
            logger.warning("send_response_without_id", method=method)
        return SendResult(provider_message_id=parsed.id_message)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._media_client.aclose()
        await self._download_client.aclose()
