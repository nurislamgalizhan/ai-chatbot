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
from pydantic import ValidationError

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
        timeout: float | None = None,
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
        return response.json()

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
