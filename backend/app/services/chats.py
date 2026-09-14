"""Chat domain operations shared by the inbox processor and the admin API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import Chat, ChatMemory, Message
from app.models.enums import (
    AttentionClearedReason,
    AttentionReason,
    LastActor,
    MsgDirection,
    MsgSource,
    MsgState,
)
from app.security.audit import record_audit
from app.services.chat_modes import on_operator_message
from app.whatsapp.outbox import enqueue_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.enums import MediaType

_MESSAGE_DEDUPE_WHERE = text("provider_message_id IS NOT NULL")


def _now() -> datetime:
    return datetime.now(UTC)


async def upsert_chat_for_incoming(
    db: AsyncSession,
    instance_pk: int,
    wa_chat_id: str,
    phone: str,
    display_name: str | None,
) -> int:
    """Create/update the chat for a customer message.

    A single INSERT ... ON CONFLICT statement: the unread increment and
    last_message bookkeeping are atomic, so concurrent notifications for
    one chat can neither create two chats nor lose an unread count.
    """
    stmt = (
        pg_insert(Chat)
        .values(
            instance_id=instance_pk,
            wa_chat_id=wa_chat_id,
            phone=phone,
            display_name=display_name,
            unread_count=1,
            last_actor=LastActor.CUSTOMER,
        )
        .on_conflict_do_update(
            constraint="uq_chats_wa_chat",
            set_={
                "unread_count": Chat.__table__.c.unread_count + 1,
                "last_message_at": func.now(),
                "last_actor": LastActor.CUSTOMER.value,
                "display_name": func.coalesce(
                    pg_insert(Chat).excluded.display_name, Chat.__table__.c.display_name
                ),
            },
        )
        .returning(Chat.id)
    )
    return int((await db.execute(stmt)).scalar_one())


async def upsert_chat_for_outgoing(
    db: AsyncSession, instance_pk: int, wa_chat_id: str, phone: str
) -> int:
    """Chat row for an operator/API outgoing message (no unread change)."""
    stmt = (
        pg_insert(Chat)
        .values(
            instance_id=instance_pk,
            wa_chat_id=wa_chat_id,
            phone=phone,
            last_actor=LastActor.HUMAN,
        )
        .on_conflict_do_update(
            constraint="uq_chats_wa_chat",
            set_={"last_message_at": func.now(), "last_actor": LastActor.HUMAN.value},
        )
        .returning(Chat.id)
    )
    return int((await db.execute(stmt)).scalar_one())


async def insert_inbound_message(
    db: AsyncSession,
    chat_id: int,
    *,
    source: MsgSource,
    state: MsgState,
    provider_message_id: str,
    body: str | None,
    media_type: MediaType | None,
    direction: MsgDirection = MsgDirection.IN,
    sent_at: datetime | None = None,
    correlation_id: str | None = None,
    error_code: str | None = None,
) -> int | None:
    """Insert a message from a notification; None if it already exists."""
    stmt = (
        pg_insert(Message)
        .values(
            chat_id=chat_id,
            direction=direction,
            source=source,
            provider_message_id=provider_message_id,
            body=body,
            media_type=media_type,
            state=state,
            sent_at=sent_at,
            correlation_id=correlation_id,
            error_code=error_code,
        )
        .on_conflict_do_nothing(
            index_elements=["chat_id", "provider_message_id"],
            index_where=_MESSAGE_DEDUPE_WHERE,
        )
        .returning(Message.id)
    )
    result = (await db.execute(stmt)).scalar_one_or_none()
    return int(result) if result is not None else None


async def set_attention(
    db: AsyncSession,
    chat: Chat,
    reason: AttentionReason,
    correlation_id: str | None = None,
) -> None:
    if chat.needs_attention:
        return
    chat.needs_attention = True
    chat.attention_reason = reason
    chat.attention_set_at = _now()
    chat.attention_cleared_at = None
    chat.attention_cleared_reason = None
    await record_audit(
        db,
        action="chat.attention_set",
        entity_type="chat",
        entity_id=str(chat.id),
        after={"reason": reason.value},
        correlation_id=correlation_id,
    )


async def clear_attention(
    db: AsyncSession,
    chat: Chat,
    reason: AttentionClearedReason,
    actor_user_id: int | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Clear the flag; audited only when it actually was set."""
    if not chat.needs_attention:
        return False
    chat.needs_attention = False
    chat.attention_cleared_at = _now()
    chat.attention_cleared_reason = reason.value
    # The memory flag told the model "a manager has been asked for"; once
    # a human has looked, it would otherwise stay true forever.
    await db.execute(
        update(ChatMemory)
        .where(ChatMemory.chat_id == chat.id, ChatMemory.handoff_required.is_(True))
        .values(handoff_required=False)
    )
    await record_audit(
        db,
        action="chat.attention_cleared",
        actor_user_id=actor_user_id,
        entity_type="chat",
        entity_id=str(chat.id),
        after={"reason": reason.value},
        correlation_id=correlation_id,
    )
    return True


async def send_panel_message(
    db: AsyncSession,
    chat: Chat,
    body_text: str,
    client_message_id: uuid.UUID,
    actor_user_id: int,
    correlation_id: str | None,
) -> tuple[int, bool]:
    """Queue an operator message. Idempotent by client_message_id:
    a retried request returns the existing message instead of re-sending.

    Returns (message_id, created).
    """
    stmt = (
        pg_insert(Message)
        .values(
            chat_id=chat.id,
            direction=MsgDirection.OUT,
            source=MsgSource.OPERATOR_PANEL,
            outbound_message_id=client_message_id,
            body=body_text,
            state=MsgState.GENERATED,  # text persisted BEFORE any send
            correlation_id=correlation_id,
        )
        .on_conflict_do_nothing(
            index_elements=["outbound_message_id"],
            index_where=text("outbound_message_id IS NOT NULL"),
        )
        .returning(Message.id)
    )
    message_id = (await db.execute(stmt)).scalar_one_or_none()
    if message_id is None:
        existing = (
            await db.execute(
                select(Message.id).where(Message.outbound_message_id == client_message_id)
            )
        ).scalar_one()
        return int(existing), False

    await enqueue_text(
        db, chat.instance_id, chat.wa_chat_id, body_text, client_message_id, correlation_id
    )
    chat.last_actor = LastActor.HUMAN
    chat.last_message_at = _now()
    # A human replied from the panel: mute/pause AI per settings (SPEC §7).
    await on_operator_message(
        db, chat, source=MsgSource.OPERATOR_PANEL, correlation_id=correlation_id
    )
    await record_audit(
        db,
        action="chat.panel_message_sent",
        actor_user_id=actor_user_id,
        entity_type="message",
        entity_id=str(message_id),
        after={"chat_id": chat.id},
        correlation_id=correlation_id,
    )
    return int(message_id), True
