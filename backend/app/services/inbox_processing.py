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

from app.models import Chat, InboundNotification, Message, WaOutbox
from app.models.enums import (
    AttentionClearedReason,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.services.chat_modes import on_operator_message
from app.services.chats import (
    clear_attention,
    insert_inbound_message,
    upsert_chat_for_incoming,
    upsert_chat_for_outgoing,
)
from app.services.wa_payload import parse_message, phone_from_wa_chat_id
from app.whatsapp.classify import Kind, classify

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 50


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
            await _process_one(db, row)
            row.status = "processed"
        except Exception:
            logger.exception("inbound_processing_failed", inbound_id=row.id)
            row.status = "failed"
        row.processed_at = datetime.now(UTC)
    return len(rows)


async def _process_one(db: AsyncSession, row: InboundNotification) -> None:
    body: dict[str, Any] = row.raw_payload or {}
    classified = classify(row.id, body)

    if classified.kind in (Kind.INCOMING_PRIVATE, Kind.INCOMING_STICKER):
        await _handle_incoming(db, row, body, is_sticker=classified.kind is Kind.INCOMING_STICKER)
    elif classified.kind is Kind.OUTGOING_PHONE:
        await _handle_operator_phone(db, row, body)
    elif classified.kind is Kind.OUTGOING_API:
        await _handle_api_echo(db, row, body)
    elif classified.kind is Kind.DELIVERY_STATUS:
        await _handle_delivery_status(db, body)
    # INSTANCE_EVENT rows stay as raw history for the status page.


async def _handle_incoming(
    db: AsyncSession, row: InboundNotification, body: dict[str, Any], *, is_sticker: bool
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
    # Stickers: fully ignored — no AI, no reply, but visible in history.
    state = MsgState.IGNORED if is_sticker else MsgState.QUEUED
    media = MediaType.STICKER if is_sticker else parsed.media_type
    await insert_inbound_message(
        db,
        chat_id,
        source=MsgSource.CUSTOMER,
        state=state,
        provider_message_id=provider_message_id,
        body=parsed.text,
        media_type=media,
        sent_at=_ts(parsed.timestamp),
    )


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


async def _handle_delivery_status(db: AsyncSession, body: dict[str, Any]) -> None:
    id_message = body.get("idMessage")
    status = body.get("status")
    if not id_message or not isinstance(status, str):
        return
    await db.execute(
        update(Message)
        .where(Message.provider_message_id == str(id_message))
        .values(delivery_status=status[:50])
    )


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
