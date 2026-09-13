from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WhatsappInstance(Base):
    """A provider instance. token_ref names the env variable holding the
    token — the token itself is NEVER stored in the database."""

    __tablename__ = "whatsapp_instances"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(Text)  # "green_api"
    instance_id: Mapped[str] = mapped_column(Text)
    token_ref: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_polled_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    #: Provider-reported authorization state ("authorized", "notAuthorized",
    #: "blocked", ...), refreshed only when the owner presses «Проверить
    #: подключение» — a status page must not poll a paid or rate-limited
    #: endpoint on every render.
    state: Mapped[str | None] = mapped_column(Text)
    state_checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InboundNotification(Base):
    """Inbox with at-most-once semantics.

    UNIQUE (instance_id, provider_message_id) is THE deduplication barrier
    (SPEC invariant 3): a redelivered notification hits the constraint and
    can never trigger a second processing/AI call. For notification kinds
    that legitimately repeat per message (delivery statuses), the dedupe
    key embeds the distinguishing parts — see whatsapp/classify.py.
    """

    __tablename__ = "inbound_notifications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("whatsapp_instances.id", ondelete="CASCADE")
    )
    provider_message_id: Mapped[str] = mapped_column(Text)  # dedupe key
    notification_type: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    #: Transient database failures (deadlock, serialization) are retried;
    #: this bounds that retry so a genuinely poisonous row cannot jam the
    #: inbox forever.
    process_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # stored: awaits processing by later pipeline phases;
    # ignored: terminal (groups, broadcasts, calls, instance events);
    # processed / failed: set by the processing pipeline;
    # parse_failed: the raw payload could not even be classified/stored
    #   normally — preserved here as-is, ACKed at the provider so the
    #   queue never wedges behind a poison notification.
    status: Mapped[str] = mapped_column(Text, server_default=text("'stored'"))


class WaOutbox(Base):
    """Transport-level outbox: every outgoing WhatsApp send goes through
    here. UNIQUE (outbound_message_id) makes sending idempotent — a retry
    of the enqueue or of the flush can never produce a duplicate message.
    The domain `messages` table (phase 3) references outbound_message_id.
    """

    __tablename__ = "wa_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("whatsapp_instances.id", ondelete="CASCADE")
    )
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    wa_chat_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default=text("'text'"))  # text|file
    #: customer | alert. An alert is for the OWNER's own number: DRY_RUN
    #: silences the customer channel, never the alarm bell.
    purpose: Mapped[str] = mapped_column(Text, server_default=text("'customer'"))
    body: Mapped[str | None] = mapped_column(Text)  # message text or caption
    file_ref: Mapped[str | None] = mapped_column(Text)  # local path for kind=file
    # PENDING -> SENT | SKIPPED_DRY_RUN | FAILED_RETRYABLE -> FAILED_FINAL
    state: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    retry_count: Mapped[int] = mapped_column(server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]


class SystemEvent(Base):
    """Technical events for the status page and alerting (no PII)."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    level: Mapped[str] = mapped_column(Text)  # info|warning|error|critical
    component: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
