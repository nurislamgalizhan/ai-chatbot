"""Money and agent tables (phase 6): instructions, calls, prices, budget.

The budget_ledger row is the heart of spend control: reservations are a
single conditional UPDATE on it (SPEC §9.2), so PostgreSQL serializes
concurrent spenders — application code never "checks then writes".
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    BudgetScope,
    CallType,
    KbLanguage,
    PriceUnit,
    ReservationStatus,
)


def _enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class AgentInstruction(Base):
    """Versioned system instruction; exactly one version can be active
    (partial unique index — a race of two activations is resolved by the
    database, not by application luck)."""

    __tablename__ = "agent_instructions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    style: Mapped[str | None] = mapped_column(Text)
    allowed_topics: Mapped[list[str] | None] = mapped_column(JSONB)
    forbidden_topics: Mapped[list[str] | None] = mapped_column(JSONB)
    # Keys: greeting/handoff/fallback/offtopic/blocked — plain answer texts.
    fallback_templates: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    escalation_rules: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)  # NULL = env default
    reasoning_effort: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default=text("false"))
    author_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AiCall(Base):
    """One paid provider call. cost_kzt is frozen at call time — a later
    exchange-rate change never rewrites history (SPEC §17 test)."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="SET NULL")
    )
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="SET NULL")
    )
    call_type: Mapped[CallType] = mapped_column(_enum(CallType, "ai_call_type"))
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    audio_seconds: Mapped[int] = mapped_column(server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 8), server_default=text("0"))
    cost_kzt: Mapped[Decimal] = mapped_column(Numeric(12, 4), server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    rag_chunk_ids: Mapped[list[int] | None] = mapped_column(JSONB)
    cache_hit: Mapped[bool] = mapped_column(server_default=text("false"))
    reasoning_effort: Mapped[str | None] = mapped_column(Text)
    instruction_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default=text("'ok'"))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ModelPrice(Base):
    """Price journal: the applicable price is the latest effective_from
    at or before the call moment — history is never edited in place."""

    __tablename__ = "model_prices"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    model: Mapped[str] = mapped_column(Text)
    unit: Mapped[PriceUnit] = mapped_column(_enum(PriceUnit, "price_unit"))
    price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    effective_from: Mapped[datetime]
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FxRate(Base):
    """Owner-entered exchange rates (never fetched from the internet)."""

    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    pair: Mapped[str] = mapped_column(Text)  # "USD/KZT", "RUB/KZT"
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    effective_from: Mapped[datetime]
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BudgetLedger(Base):
    """One counter row per (scope, key, period). All spenders serialize on
    it via the conditional-UPDATE reservation (SPEC §9.2)."""

    __tablename__ = "budget_ledger"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    scope: Mapped[BudgetScope] = mapped_column(_enum(BudgetScope, "budget_scope"))
    scope_key: Mapped[str] = mapped_column(Text)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BudgetReservation(Base):
    """Idempotency record for reserve/settle/release per ledger row.

    correlation_id here is a SUB-correlation ("{correlation}:main",
    "{correlation}:summary") — several paid calls for one message never
    collide on the unique key.
    """

    __tablename__ = "budget_reservations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("budget_ledger.id", ondelete="CASCADE")
    )
    correlation_id: Mapped[str] = mapped_column(Text)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    status: Mapped[ReservationStatus] = mapped_column(
        _enum(ReservationStatus, "reservation_status"),
        server_default=text("'held'"),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    settled_at: Mapped[datetime | None]


class SemanticCacheEntry(Base):
    """Answer cache keyed by normalized query + language + category +
    kb_version; bumping kb_version invalidates everything without a scan.
    category is '' (never NULL) so the unique key actually deduplicates."""

    __tablename__ = "semantic_cache"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    normalized_query_hash: Mapped[str] = mapped_column(Text)
    language: Mapped[KbLanguage] = mapped_column(
        SAEnum(
            KbLanguage,
            name="kb_language",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        )
    )
    category: Mapped[str] = mapped_column(Text, server_default=text("''"))
    kb_version: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str] = mapped_column(Text)
    hit_count: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]


class AlertNotification(Base):
    """Dedup memory for "once per threshold" alerts (SPEC §9.3). Written
    in phase 6; actual WhatsApp delivery to alert_phone lands in phase 9."""

    __tablename__ = "alert_notifications"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # budget_day / budget_month / system
    event_key: Mapped[str] = mapped_column(Text)  # "80pct", "circuit_breaker", ...
    period_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
