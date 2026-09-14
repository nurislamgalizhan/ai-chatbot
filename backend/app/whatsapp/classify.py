"""Pure classification of GREEN-API notifications.

The single place that decides what a notification IS. The AI pipeline may
only ever be triggered by INCOMING_PRIVATE (SPEC invariant 7); groups and
non-private chats are fully ignored (owner decision: the bot neither reads
nor writes in groups); stickers are stored but never answered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    INCOMING_PRIVATE = "incoming_private"  # the ONLY kind that may reach AI
    INCOMING_STICKER = "incoming_sticker"  # stored; never answered, never AI
    #: A REACTION to a message (long press -> 👍), not a message. Stored so
    #: the history is true, never answered, never paid for. This is NOT
    #: about emoji inside a text message: "Здравствуйте 😊 сколько стоит?"
    #: is an ordinary question and is answered as one.
    INCOMING_REACTION = "incoming_reaction"
    #: The customer EDITED a message they had already sent. The new text
    #: is the question now — answered like any other.
    INCOMING_EDITED = "incoming_edited"
    #: The customer DELETED a message. Whatever we had not answered yet
    #: must not be answered now.
    INCOMING_DELETED = "incoming_deleted"
    #: Location, contact card, poll: real content the bot cannot act on.
    #: Goes to a human with honest wording, never called "a file".
    INCOMING_UNSUPPORTED = "incoming_unsupported"
    INCOMING_GROUP = "incoming_group"  # fully ignored, no chat is created
    INCOMING_OTHER_CHAT = "incoming_other_chat"  # broadcast/newsletter: ignored
    OUTGOING_PHONE = "outgoing_phone"  # a human replied from the phone
    OUTGOING_API = "outgoing_api"  # our own send echoed back
    DELIVERY_STATUS = "delivery_status"
    CALL = "call"
    INSTANCE_EVENT = "instance_event"
    #: The provider says the plan's quota is exhausted: chats stop working.
    QUOTA_EXCEEDED = "quota_exceeded"
    UNKNOWN = "unknown"


# Kinds that are terminal at the inbox: nothing will ever process them.
IGNORED_KINDS = frozenset({Kind.INCOMING_GROUP, Kind.INCOMING_OTHER_CHAT, Kind.CALL, Kind.UNKNOWN})

_PRIVATE_SUFFIX = "@c.us"
_GROUP_SUFFIX = "@g.us"
_STICKER_TYPES = frozenset({"stickerMessage"})
#: A reaction arrives as an ordinary incomingMessageReceived and differs
#: only by typeMessage. Treating it as content is how a 👍 on the bot's
#: answer turned into "I passed your file to a manager" (and cost money).
_REACTION_TYPES = frozenset({"reactionMessage"})
_EDITED_TYPES = frozenset({"editedMessage"})
_DELETED_TYPES = frozenset({"deletedMessage"})
#: Content a text bot cannot use, but which IS a deliberate message from
#: the customer: a pin on the map, a contact card, a poll.
_UNSUPPORTED_CONTENT_TYPES = frozenset(
    {
        "locationMessage",
        "contactMessage",
        "contactsArrayMessage",
        "pollMessage",
        "pollUpdateMessage",
        "groupInviteMessage",
        "buttonsResponseMessage",
        "templateButtonsReplyMessage",
        "listResponseMessage",
        "interactiveButtonsReply",
    }
)


@dataclass(frozen=True)
class Classified:
    kind: Kind
    notification_type: str
    dedupe_key: str
    chat_id: str | None


def _dict(value: object) -> dict[str, Any]:
    """Untrusted payload: any nested part may be of the wrong type."""
    return value if isinstance(value, dict) else {}


def _chat_id(body: dict[str, Any]) -> str | None:
    chat_id = _dict(body.get("senderData")).get("chatId") or body.get("chatId")
    return str(chat_id) if chat_id else None


def _message_type(body: dict[str, Any]) -> str | None:
    type_message = _dict(body.get("messageData")).get("typeMessage")
    return str(type_message) if type_message else None


def _incoming_kind(message_type: str | None) -> Kind:
    """What a private incoming notification actually IS.

    One lookup per WhatsApp message type. Everything unknown stays
    INCOMING_PRIVATE on purpose: an unfamiliar type that carries text is
    better answered than silently dropped, and the text extractor decides
    whether there is anything to answer.
    """
    if message_type in _STICKER_TYPES:
        return Kind.INCOMING_STICKER
    if message_type in _REACTION_TYPES:
        return Kind.INCOMING_REACTION
    if message_type in _EDITED_TYPES:
        return Kind.INCOMING_EDITED
    if message_type in _DELETED_TYPES:
        return Kind.INCOMING_DELETED
    if message_type in _UNSUPPORTED_CONTENT_TYPES:
        return Kind.INCOMING_UNSUPPORTED
    return Kind.INCOMING_PRIVATE


def _fallback_dedupe_key(receipt_id: int, body: dict[str, Any]) -> str:
    # Notifications without idMessage (instance events, calls): the receipt
    # id plus the provider timestamp is stable across redeliveries.
    return f"receipt:{receipt_id}:{body.get('timestamp', '')}"


def classify(receipt_id: int, body: dict[str, Any]) -> Classified:  # noqa: PLR0911 - dispatch
    webhook_type = str(body.get("typeWebhook", ""))
    id_message = body.get("idMessage")
    chat_id = _chat_id(body)

    if webhook_type == "incomingMessageReceived":
        dedupe = str(id_message) if id_message else _fallback_dedupe_key(receipt_id, body)
        if chat_id is None or not chat_id.endswith(_PRIVATE_SUFFIX):
            kind = (
                Kind.INCOMING_GROUP
                if (chat_id or "").endswith(_GROUP_SUFFIX)
                else (Kind.INCOMING_OTHER_CHAT)
            )
            return Classified(kind, webhook_type, dedupe, chat_id)
        return Classified(_incoming_kind(_message_type(body)), webhook_type, dedupe, chat_id)

    if webhook_type == "outgoingMessageReceived":
        dedupe = str(id_message) if id_message else _fallback_dedupe_key(receipt_id, body)
        return Classified(Kind.OUTGOING_PHONE, webhook_type, dedupe, chat_id)

    if webhook_type == "outgoingAPIMessageReceived":
        dedupe = str(id_message) if id_message else _fallback_dedupe_key(receipt_id, body)
        return Classified(Kind.OUTGOING_API, webhook_type, dedupe, chat_id)

    if webhook_type == "outgoingMessageStatus":
        # Several statuses per message (sent -> delivered -> read) share one
        # idMessage: the status is part of the dedupe key on purpose.
        status = body.get("status", "")
        dedupe = f"{id_message}:status:{status}"
        return Classified(Kind.DELIVERY_STATUS, webhook_type, dedupe, chat_id)

    if webhook_type in {"incomingCall", "outgoingCall"}:
        return Classified(Kind.CALL, webhook_type, _fallback_dedupe_key(receipt_id, body), chat_id)

    if webhook_type == "quotaExceeded":
        # The plan's limit is reached: the provider stops serving chats.
        # Silently storing this (it used to land in UNKNOWN) meant the bot
        # went quiet and the owner learned it from a customer.
        return Classified(
            Kind.QUOTA_EXCEEDED, webhook_type, _fallback_dedupe_key(receipt_id, body), chat_id
        )

    if webhook_type in {"stateInstanceChanged", "statusInstanceChanged", "deviceInfo"}:
        return Classified(
            Kind.INSTANCE_EVENT, webhook_type, _fallback_dedupe_key(receipt_id, body), chat_id
        )

    return Classified(
        Kind.UNKNOWN, webhook_type or "unknown", _fallback_dedupe_key(receipt_id, body), chat_id
    )
