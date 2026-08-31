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


def parse_message(body: dict[str, Any]) -> ParsedMessage:
    message_data = _dict(body.get("messageData"))
    type_message = str(message_data.get("typeMessage", ""))

    text: str | None = None
    media: MediaType | None = None

    if type_message in _TEXT_TYPES:
        text = _clip(
            _dict(message_data.get("textMessageData")).get("textMessage")
            or _dict(message_data.get("extendedTextMessageData")).get("text"),
            _MAX_TEXT_LEN,
        )
    elif type_message in _MEDIA_BY_TYPE:
        media = _MEDIA_BY_TYPE[type_message]
        # Caption travels with the file; the file itself is phase 7.
        text = _clip((message_data.get("fileMessageData") or {}).get("caption"), _MAX_TEXT_LEN)
    elif type_message:
        media = MediaType.OTHER

    sender_data = body.get("senderData") or {}
    sender_name = _clip(sender_data.get("senderName") or sender_data.get("chatName"), _MAX_NAME_LEN)
    raw_ts = body.get("timestamp")
    timestamp = int(raw_ts) if isinstance(raw_ts, int | float) else None
    return ParsedMessage(text=text, media_type=media, sender_name=sender_name, timestamp=timestamp)
