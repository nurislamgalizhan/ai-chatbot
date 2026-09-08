"""Parsing of GREEN-API message payloads into domain values.

Provider payloads are UNTRUSTED input: everything is size-capped and
defensively typed; unknown message types degrade to media_type=other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.enums import MediaType

_MAX_TEXT_LEN = 8000
_MAX_NAME_LEN = 200
_MAX_URL_LEN = 2000
_MAX_MIME_LEN = 120

_MEDIA_BY_TYPE: dict[str, MediaType] = {
    "audioMessage": MediaType.VOICE,
    "imageMessage": MediaType.IMAGE,
    "videoMessage": MediaType.VIDEO,
    "documentMessage": MediaType.DOCUMENT,
    "stickerMessage": MediaType.STICKER,
}
_TEXT_TYPES = frozenset({"textMessage", "extendedTextMessage", "quotedMessage"})


@dataclass(frozen=True)
class ParsedMessage:
    text: str | None
    media_type: MediaType | None
    sender_name: str | None
    timestamp: int | None
    # Media metadata (phase 7). The provider's file name is kept only for
    # display/extension hints — it NEVER becomes a path on disk (traversal).
    download_url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None


def phone_from_wa_chat_id(wa_chat_id: str) -> str:
    """ "77011234567@c.us" -> "+77011234567"."""
    digits = wa_chat_id.split("@", 1)[0]
    return f"+{digits}"


def _dict(value: object) -> dict[str, Any]:
    """Untrusted payload: any nested part may be of the wrong type."""
    return value if isinstance(value, dict) else {}


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _https_url(value: object) -> str | None:
    """Only https download URLs are accepted: the payload is untrusted,
    and file:// or http:// there would turn the downloader into an SSRF
    or plaintext-fetch primitive."""
    url = _clip(value, _MAX_URL_LEN)
    if url is None or not url.lower().startswith("https://"):
        return None
    return url


def parse_message(body: dict[str, Any]) -> ParsedMessage:
    message_data = _dict(body.get("messageData"))
    type_message = str(message_data.get("typeMessage", ""))

    text: str | None = None
    media: MediaType | None = None
    download_url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None

    if type_message in _TEXT_TYPES:
        text = _clip(
            _dict(message_data.get("textMessageData")).get("textMessage")
            or _dict(message_data.get("extendedTextMessageData")).get("text"),
            _MAX_TEXT_LEN,
        )
    elif type_message in _MEDIA_BY_TYPE:
        media = _MEDIA_BY_TYPE[type_message]
        file_data = _dict(message_data.get("fileMessageData"))
        # Caption travels with the file.
        text = _clip(file_data.get("caption"), _MAX_TEXT_LEN)
        download_url = _https_url(file_data.get("downloadUrl"))
        file_name = _clip(file_data.get("fileName"), _MAX_NAME_LEN)
        mime_type = _clip(file_data.get("mimeType"), _MAX_MIME_LEN)
    elif type_message:
        media = MediaType.OTHER

    sender_data = body.get("senderData") or {}
    sender_name = _clip(sender_data.get("senderName") or sender_data.get("chatName"), _MAX_NAME_LEN)
    raw_ts = body.get("timestamp")
    timestamp = int(raw_ts) if isinstance(raw_ts, int | float) else None
    return ParsedMessage(
        text=text,
        media_type=media,
        sender_name=sender_name,
        timestamp=timestamp,
        download_url=download_url,
        file_name=file_name,
        mime_type=mime_type,
    )
