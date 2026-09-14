"""Turns stored inbound notifications into domain chats and messages.

Runs in the worker. AI is NEVER triggered here — customer messages are
merely persisted as QUEUED for the (phase 6) pipeline; everything else is
bookkeeping. Stickers become IGNORED messages: visible in the panel, never
answered (owner decision). A human's reply from the phone clears the
"needs attention" flag (owner decision).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.models import Chat, InboundNotification, Message, SystemEvent, WaOutbox
from app.models.enums import (
    AttentionClearedReason,
    AttentionReason,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services import app_settings
from app.services.alerts import hour_bucket, is_alert_chat, record_alert
from app.services.app_settings import get_setting
from app.services.chat_modes import on_operator_message
from app.services.chats import (
    clear_attention,
    insert_inbound_message,
    set_attention,
    upsert_chat_for_incoming,
    upsert_chat_for_outgoing,
)
from app.services.media_processing import enqueue_media_job
from app.services.wa_payload import parse_message, phone_from_wa_chat_id
from app.whatsapp.classify import Kind, classify

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 50

#: Incoming kinds that become a message row. Reactions and stickers are
#: here too — they belong in the history — but they are never queued for
#: the AI (see `_handle_incoming`).
_STORED_INCOMING_KINDS = frozenset(
    {
        Kind.INCOMING_PRIVATE,
        Kind.INCOMING_STICKER,
        Kind.INCOMING_REACTION,
        Kind.INCOMING_EDITED,
        Kind.INCOMING_UNSUPPORTED,
    }
)


def _ts(parsed_timestamp: int | None) -> datetime | None:
    if parsed_timestamp is None:
        return None
    return datetime.fromtimestamp(parsed_timestamp, tz=UTC)


async def process_inbound_batch(db: AsyncSession) -> int:
    """Process up to a batch of stored notifications; returns the count."""
    rows = (
        (
            await db.execute(
                select(InboundNotification)
                .where(InboundNotification.status == "stored")
                .order_by(InboundNotification.id)
                .limit(_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        try:
            # SAVEPOINT per notification. A PostgreSQL-level error (a FK
            # violation, a deadlock against the pipeline's SELECT FOR
            # UPDATE on the same chat) poisons the whole transaction:
            # without this, the batch could neither be committed nor
            # marked failed, the rows stayed `stored`, and the inbox
            # jammed forever on one bad notification — the bot goes
            # silent for every customer.
            async with db.begin_nested():
                await _process_one(db, row)
            row.status = "processed"
        except Exception as exc:
            logger.exception("inbound_processing_failed", inbound_id=row.id)
            if _is_transient(exc) and row.process_attempts < _MAX_PROCESS_ATTEMPTS:
                # A deadlock or serialization failure is the database
                # asking us to try again, not a broken notification.
                # Dropping a customer's message on one of those would
                # lose a real question forever.
                row.process_attempts += 1
                continue
            row.status = "failed"
            db.add(
                SystemEvent(
                    level="error",
                    component="inbox",
                    event="inbound_processing_failed",
                    detail={
                        "inbound_id": row.id,
                        "type": row.notification_type,
                        "error": type(exc).__name__,
                    },
                )
            )
        row.processed_at = datetime.now(UTC)
    return len(rows)


#: PostgreSQL asks the client to retry these; they say nothing about the
#: notification itself. 40001 = serialization failure, 40P01 = deadlock.
_RETRY_SQLSTATES = {"40001", "40P01", "55P03", "57014"}
#: After this many transient failures the row is treated as poison
#: anyway: an endless retry would jam the inbox just as badly.
_MAX_PROCESS_ATTEMPTS = 5


def _is_transient(exc: BaseException) -> bool:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
        exc, "sqlstate", None
    )
    if sqlstate in _RETRY_SQLSTATES:
        return True
    # Connection-level failures (database restarted mid-batch) are
    # transient by definition.
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


async def _process_one(db: AsyncSession, row: InboundNotification) -> None:
    body: dict[str, Any] = row.raw_payload or {}
    classified = classify(row.id, body)

    # The owner's alert number is not a customer. Our own alert echoes
    # back as an outgoing notification (it would open a "chat" with the
    # owner's personal number), and a reply to an alert would arrive as
    # an incoming message — answered by the AI, paid for, and counted as
    # a customer conversation.
    wa_chat_id = str((body.get("senderData") or {}).get("chatId", ""))
    if wa_chat_id and is_alert_chat(
        wa_chat_id, str(await get_setting(db, app_settings.ALERT_PHONE) or "")
    ):
        logger.info("alert_channel_notification_ignored", inbound_id=row.id)
        return

    if classified.kind in _STORED_INCOMING_KINDS:
        await _handle_incoming(db, row, body, kind=classified.kind)
    elif classified.kind is Kind.INCOMING_DELETED:
        await _handle_deleted(db, row, body)
    elif classified.kind is Kind.OUTGOING_PHONE:
        await _handle_operator_phone(db, row, body)
    elif classified.kind is Kind.OUTGOING_API:
        await _handle_api_echo(db, row, body)
    elif classified.kind is Kind.DELIVERY_STATUS:
        await _handle_delivery_status(db, body)
    elif classified.kind is Kind.QUOTA_EXCEEDED:
        await _handle_quota_exceeded(db, body)
    # INSTANCE_EVENT rows stay as raw history for the status page.


async def _handle_incoming(
    db: AsyncSession, row: InboundNotification, body: dict[str, Any], *, kind: Kind
) -> None:
    wa_chat_id = str((body.get("senderData") or {}).get("chatId", ""))
    parsed = parse_message(body)
    provider_message_id = str(body.get("idMessage") or row.provider_message_id)
    chat_id = await upsert_chat_for_incoming(
        db,
        row.instance_id,
        wa_chat_id,
        phone_from_wa_chat_id(wa_chat_id),
        parsed.sender_name,
    )
    # Stickers and REACTIONS are stored so the history is true, and never
    # answered: a reaction is not a question. This is about the reaction
    # notification type only — a text message containing emoji
    # ("Здравствуйте 😊 сколько стоит зал?") is an ordinary question and
    # takes the normal QUEUED path below.
    silent = kind in (Kind.INCOMING_STICKER, Kind.INCOMING_REACTION)
    state = MsgState.IGNORED if silent else MsgState.QUEUED
    media: MediaType | None
    if kind is Kind.INCOMING_STICKER:
        media = MediaType.STICKER
    elif kind is Kind.INCOMING_REACTION:
        media = MediaType.OTHER
    else:
        media = parsed.media_type
    body_text = parsed.text
    if kind is Kind.INCOMING_REACTION:
        emoji = (
            _dict_get(body, "messageData", "extendedTextMessageData", "text")
            or _dict_get(body, "messageData", "reactionMessageData", "text")
            or ""
        )
        body_text = f"[реакция] {emoji}".strip()
    message_id = await insert_inbound_message(
        db,
        chat_id,
        source=MsgSource.CUSTOMER,
        state=state,
        provider_message_id=provider_message_id,
        body=body_text,
        media_type=media,
        error_code="reaction" if kind is Kind.INCOMING_REACTION else None,
        sent_at=_ts(parsed.timestamp),
    )
    # Voice and images need a paid pre-processing step before the AI can
    # answer them; the job carries the download URL, which is available
    # only here, while the raw payload is at hand (phase 7). Everything
    # else (documents, video, gif, stickers, reactions) never goes to
    # OpenAI.
    if message_id is not None and media is not None and not silent:
        await enqueue_media_job(
            db,
            message_id=message_id,
            chat_id=chat_id,
            media_type=media,
            download_url=parsed.download_url,
            mime_type=parsed.mime_type,
        )


def _dict_get(body: dict[str, Any], *path: str) -> str:
    """Read a nested string from an untrusted payload."""
    current: Any = body
    for key in path:
        current = current.get(key) if isinstance(current, dict) else None
    return str(current) if isinstance(current, str) else ""


async def _handle_deleted(db: AsyncSession, row: InboundNotification, body: dict[str, Any]) -> None:
    """The customer deleted a message.

    If we had not answered it yet, we must not answer it now: replying to
    a retracted question is worse than staying silent. The deletion
    itself is not shown as a new message — it changes the old one.
    """
    stanza_id = _dict_get(body, "messageData", "deletedMessageData", "stanzaId")
    if not stanza_id:
        return
    result = await db.execute(
        update(Message)
        .where(
            Message.provider_message_id == stanza_id,
            Message.direction == MsgDirection.IN,
            Message.state.in_((MsgState.QUEUED, MsgState.RECEIVED)),
        )
        .values(state=MsgState.IGNORED, error_code="deleted_by_customer", next_attempt_at=None)
    )
    cancelled = int(getattr(result, "rowcount", 0) or 0)
    logger.info("customer_deleted_message", inbound_id=row.id, cancelled=cancelled)


async def _handle_quota_exceeded(db: AsyncSession, body: dict[str, Any]) -> None:
    """The provider's plan limit is reached: chats stop working.

    Recorded AND alerted: this is precisely the state where the bot looks
    healthy from the inside and delivers nothing.
    """
    raw_quota = body.get("quotaData")
    quota: dict[str, Any] = raw_quota if isinstance(raw_quota, dict) else {}
    detail = {
        key: quota[key]
        for key in ("method", "used", "total", "status")
        if quota.get(key) is not None
    }
    db.add(
        SystemEvent(
            level="critical",
            component="receiver",
            event="provider_quota_exceeded",
            detail=detail,
        )
    )
    await record_alert(
        db,
        kind="system",
        event_key="quota_exceeded",
        period_key=hour_bucket(),
        detail=detail,
    )
    logger.error("provider_quota_exceeded", **detail)


async def _handle_operator_phone(
    db: AsyncSession, row: InboundNotification, body: dict[str, Any]
) -> None:
    wa_chat_id = str((body.get("senderData") or {}).get("chatId", ""))
    if not wa_chat_id.endswith("@c.us"):
        return  # a human writing into a group — none of our business
    parsed = parse_message(body)
    chat_id = await upsert_chat_for_outgoing(
        db, row.instance_id, wa_chat_id, phone_from_wa_chat_id(wa_chat_id)
    )
    await insert_inbound_message(
        db,
        chat_id,
        source=MsgSource.OPERATOR_PHONE,
        state=MsgState.SENT,
        provider_message_id=str(body.get("idMessage") or row.provider_message_id),
        body=parsed.text,
        media_type=parsed.media_type,
        direction=MsgDirection.OUT,
        sent_at=_ts(parsed.timestamp),
    )
    # Owner decision: a manager's reply from the phone clears the flag.
    chat = await db.get(Chat, chat_id)
    if chat is not None:
        await clear_attention(db, chat, AttentionClearedReason.OPERATOR_PHONE_REPLY)
        # ...and mutes/pauses AI for this chat (SPEC §7).
        await on_operator_message(db, chat, source=MsgSource.OPERATOR_PHONE)


async def _handle_api_echo(
    db: AsyncSession, row: InboundNotification, body: dict[str, Any]
) -> None:
    """Our own API send coming back as a notification. If it matches an
    outbox row it is already represented by a domain message — only link
    the provider id. An unmatched echo (sent by some other tool through
    the same instance) becomes a `system` message so history stays true.
    """
    id_message = str(body.get("idMessage"))
    outbox_row = (
        await db.execute(select(WaOutbox).where(WaOutbox.provider_message_id == id_message))
    ).scalar_one_or_none()
    if outbox_row is not None:
        await db.execute(
            update(Message)
            .where(Message.outbound_message_id == outbox_row.outbound_message_id)
            .values(provider_message_id=id_message)
        )
        return

    wa_chat_id = str((body.get("senderData") or {}).get("chatId", ""))
    if not wa_chat_id.endswith("@c.us"):
        return
    parsed = parse_message(body)
    chat_id = await upsert_chat_for_outgoing(
        db, row.instance_id, wa_chat_id, phone_from_wa_chat_id(wa_chat_id)
    )
    await insert_inbound_message(
        db,
        chat_id,
        source=MsgSource.SYSTEM,
        state=MsgState.SENT,
        provider_message_id=id_message,
        body=parsed.text,
        media_type=parsed.media_type,
        direction=MsgDirection.OUT,
        sent_at=_ts(parsed.timestamp),
    )


#: Delivery statuses that mean the customer did NOT get the message.
_FAILED_STATUSES = frozenset({"failed", "noAccount", "notInGroup"})
#: The provider suspended the number for spam-like activity. This is not
#: one message failing — it is the whole channel at risk.
_YELLOW_CARD = "yellowCard"


async def _handle_delivery_status(db: AsyncSession, body: dict[str, Any]) -> None:
    id_message = body.get("idMessage")
    status = body.get("status")
    if not id_message or not isinstance(status, str):
        return
    message = (
        await db.execute(
            select(Message).where(Message.provider_message_id == str(id_message)).limit(1)
        )
    ).scalar_one_or_none()
    if message is None:
        return
    message.delivery_status = status[:50]

    if status == _YELLOW_CARD:
        # Recorded and alerted: losing this number takes the whole bot
        # down, and the owner can only react if they are told.
        db.add(
            SystemEvent(
                level="critical",
                component="transport",
                event="number_suspended",
                detail={"status": status},
            )
        )
        await record_alert(db, kind="system", event_key="yellow_card", period_key=hour_bucket())
        logger.error("number_suspended", status=status)
        return

    if status not in _FAILED_STATUSES:
        return

    # The answer was written, sent, and never arrived. Nobody would ever
    # notice: the panel used to store the status and show nothing.
    chat = await db.get(Chat, message.chat_id)
    if chat is not None:
        await set_attention(db, chat, AttentionReason.PROCESSING_ERROR, message.correlation_id)
    db.add(
        SystemEvent(
            level="error",
            component="transport",
            event="delivery_failed",
            detail={"status": status, "chat_id": message.chat_id},
            correlation_id=message.correlation_id,
        )
    )
    logger.warning("delivery_failed", status=status, chat_id=message.chat_id)


async def reconcile_outbox(db: AsyncSession) -> int:
    """Propagate terminal outbox states onto domain messages.

    Keeps transport decoupled from the domain: the flusher only touches
    wa_outbox; this reconciler owns the message-side bookkeeping.
    """
    rows = (
        await db.execute(
            select(Message, WaOutbox)
            .join(WaOutbox, WaOutbox.outbound_message_id == Message.outbound_message_id)
            .where(
                Message.state == MsgState.GENERATED,
                Message.delivery_status.is_(None),
                WaOutbox.state.in_(("SENT", "SKIPPED_DRY_RUN", "FAILED_FINAL")),
            )
            .with_for_update(skip_locked=True, of=Message)
        )
    ).all()
    for message, outbox_row in rows:
        if outbox_row.state == "SENT":
            message.state = MsgState.SENT
            message.sent_at = outbox_row.sent_at
            message.provider_message_id = outbox_row.provider_message_id
        elif outbox_row.state == "SKIPPED_DRY_RUN":
            # Honest state: processed but deliberately not sent (DRY_RUN).
            message.delivery_status = "skipped_dry_run"
        else:  # FAILED_FINAL
            message.state = MsgState.FAILED_FINAL
            message.error_code = outbox_row.error_code
    return len(rows)
