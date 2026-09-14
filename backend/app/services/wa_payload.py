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
#: An edited message carries the NEW text in its own object. It arrives as
#: an ordinary incoming notification, so without this the customer's
#: corrected question had no text at all and was never answered.
_EDITED_TYPE = "editedMessage"


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


#: Content types we summarise in words for the manager.
_DESCRIBED_TYPES = frozenset(
    {"locationMessage", "contactMessage", "contactsArrayMessage", "pollMessage"}
)


def _describe_content(type_message: str, message_data: dict[str, Any]) -> str:
    """A one-line Russian description of non-text content.

    Written into the message body so the manager opening the chat sees
    "клиент отправил геолокацию" and not an empty bubble. The customer's
    own data is preserved (an address, a phone) — this text stays in OUR
    database and is never sent to the model as an instruction.
    """
    if type_message == "locationMessage":
        location = _dict(message_data.get("locationMessageData"))
        parts = [
            str(location.get("nameLocation") or "").strip(),
            str(location.get("address") or "").strip(),
        ]
        where = ", ".join(part for part in parts if part)
        coords = f"{location.get('latitude')}, {location.get('longitude')}"
        return _clip(f"[геолокация] {where or coords}", _MAX_TEXT_LEN) or "[геолокация]"
    if type_message in {"contactMessage", "contactsArrayMessage"}:
        contact = _dict(message_data.get("contactMessageData"))
        name = str(contact.get("displayName") or "").strip()
        return _clip(f"[контакт] {name}".strip(), _MAX_TEXT_LEN) or "[контакт]"
    poll = _dict(message_data.get("pollMessageData"))
    name = str(poll.get("name") or "").strip()
    return _clip(f"[опрос] {name}".strip(), _MAX_TEXT_LEN) or "[опрос]"


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
    elif type_message == _EDITED_TYPE:
        text = _clip(_dict(message_data.get("editedMessageData")).get("textMessage"), _MAX_TEXT_LEN)
    elif type_message in _MEDIA_BY_TYPE:
        media = _MEDIA_BY_TYPE[type_message]
        file_data = _dict(message_data.get("fileMessageData"))
        # Caption travels with the file.
        text = _clip(file_data.get("caption"), _MAX_TEXT_LEN)
        download_url = _https_url(file_data.get("downloadUrl"))
        file_name = _clip(file_data.get("fileName"), _MAX_NAME_LEN)
        mime_type = _clip(file_data.get("mimeType"), _MAX_MIME_LEN)
    elif type_message in _DESCRIBED_TYPES:
        # Real content the bot cannot use, but a human can: the history
        # must say WHAT arrived instead of calling everything "a file".
        text = _describe_content(type_message, message_data)
        media = MediaType.OTHER
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
