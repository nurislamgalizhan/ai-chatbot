from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Numeric,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AnswerSource,
    AttentionReason,
    ChatMode,
    LastActor,
    MediaType,
    MsgDirection,
    MsgSource,
    MsgState,
)


def _enum(enum_cls: type, name: str) -> SAEnum:
    # Store enum VALUES (lowercase where defined so), not member names.
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class Chat(Base):
    """One private WhatsApp conversation. Group chats never get a row
    (owner decision: the bot neither reads nor writes in groups)."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("whatsapp_instances.id", ondelete="CASCADE")
    )
    wa_chat_id: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)  # E.164
    display_name: Mapped[str | None] = mapped_column(Text)

    mode: Mapped[ChatMode] = mapped_column(
        _enum(ChatMode, "chat_mode"), server_default=text("'AUTO_AI'")
    )
    paused_until: Mapped[datetime | None]
    mode_changed_by: Mapped[str | None] = mapped_column(Text)
    mode_changed_at: Mapped[datetime | None]
    mode_change_reason: Mapped[str | None] = mapped_column(Text)

    is_resolved: Mapped[bool] = mapped_column(server_default=text("false"))
    unread_count: Mapped[int] = mapped_column(server_default=text("0"))
    last_message_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_actor: Mapped[LastActor | None] = mapped_column(_enum(LastActor, "last_actor"))
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))

    # "Requires attention" replaces a separate lead/request entity
    # (owner decision). Cleared automatically when a human replies from
    # the phone or opens the dialog in the panel.
    needs_attention: Mapped[bool] = mapped_column(server_default=text("false"))
    attention_reason: Mapped[AttentionReason | None] = mapped_column(
        _enum(AttentionReason, "attention_reason")
    )
    attention_set_at: Mapped[datetime | None]
    attention_cleared_at: Mapped[datetime | None]
    attention_cleared_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChatModeHistory(Base):
    """Journal of mode transitions: who switched, why, until when.
    Timer extensions update chats.paused_until without a new row here —
    only actual FROM->TO transitions are recorded."""

    __tablename__ = "chat_mode_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    from_mode: Mapped[ChatMode] = mapped_column(_enum(ChatMode, "chat_mode"))
    to_mode: Mapped[ChatMode] = mapped_column(_enum(ChatMode, "chat_mode"))
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    paused_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    direction: Mapped[MsgDirection] = mapped_column(_enum(MsgDirection, "msg_direction"))
    source: Mapped[MsgSource] = mapped_column(_enum(MsgSource, "msg_source"))
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    body: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[MediaType | None] = mapped_column(_enum(MediaType, "media_type"))
    media_ref: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    state: Mapped[MsgState] = mapped_column(_enum(MsgState, "msg_state"))
    retry_count: Mapped[int] = mapped_column(server_default=text("0"))
    # Backoff scheduling for the AI pipeline: a retryable failure sets a
    # future timestamp instead of hot-looping on the same message.
    next_attempt_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    # How an AI answer was produced (NULL for everything else): the only
    # trace a free FAQ/cache answer leaves behind.
    answer_source: Mapped[AnswerSource | None] = mapped_column(_enum(AnswerSource, "answer_source"))
    delivery_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]


class ChatMemory(Base):
    """Structured per-chat memory; one row per chat, updated cheaply."""

    __tablename__ = "chat_memory"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str | None] = mapped_column(Text)  # ru|kk|en|mixed
    interest: Mapped[str | None] = mapped_column(Text)
    requested_date: Mapped[str | None] = mapped_column(Text)
    requested_time: Mapped[str | None] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text)
    handoff_required: Mapped[bool] = mapped_column(server_default=text("false"))
    last_topic: Mapped[str | None] = mapped_column(Text)
    summary_text: Mapped[str | None] = mapped_column(Text)
    summary_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class InternalNote(Base):
    """Operator notes; NEVER visible to the customer."""

    __tablename__ = "internal_notes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="CASCADE")
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChatTag(Base):
    __tablename__ = "chat_tags"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
