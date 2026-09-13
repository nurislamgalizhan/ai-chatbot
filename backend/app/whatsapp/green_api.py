"""GREEN-API implementation of WhatsAppProvider.

Security note: GREEN-API puts the token into the URL path. Every error is
re-raised as a sanitized ProviderError that carries neither URL nor token,
and httpx logging stays disabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from httpx._client import UseClientDefault
from pydantic import ValidationError

from app.whatsapp.download_guard import DownloadNotAllowedError, check_download_url
from app.whatsapp.provider import (
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


def _classify_status(method: str, status_code: int) -> ProviderError:
    return ProviderError(
        method, status_code=status_code, retryable=status_code in _RETRYABLE_STATUS
    )


class GreenApiProvider(WhatsAppProvider):
    supports_typing = False  # GREEN-API exposes no typing indicator

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

    async def send_text(self, chat_id: str, text: str) -> SendResult:
        payload = await self._request(
            "sendMessage",
            "POST",
            self._path("sendMessage"),
            json={"chatId": chat_id, "message": text},
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
            check_download_url(url, self._download_hosts)
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
