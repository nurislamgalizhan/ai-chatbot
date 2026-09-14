"""Prices, exchange rates and cost math.

Costs are computed from FACTUAL usage returned by the API; estimates are
used only for reservations (SPEC §9.1). cost_kzt is frozen with the rate
effective at call time — later rate changes never rewrite history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models import FxRate, ModelPrice
from app.models.enums import PriceUnit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings

USD_KZT = "USD/KZT"

_MILLION = Decimal(1_000_000)
_USD_PLACES = Decimal("0.00000001")
_KZT_PLACES = Decimal("0.0001")


class PricingError(Exception):
    """Prices or rate not configured — money math must fail closed."""


@dataclass(frozen=True)
class PriceSheet:
    """USD per 1M tokens for one chat model at one moment."""

    model: str
    input_usd: Decimal
    cached_input_usd: Decimal
    output_usd: Decimal


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


async def load_price_sheet(db: AsyncSession, model: str, at: datetime | None = None) -> PriceSheet:
    """The applicable price per unit is the latest effective_from <= at.

    Raises PricingError when any of the three units is missing: guessing
    a price would silently corrupt every downstream number.
    """
    moment = at or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(ModelPrice)
                .where(ModelPrice.model == model, ModelPrice.effective_from <= moment)
                .order_by(ModelPrice.unit, ModelPrice.effective_from.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[PriceUnit, Decimal] = {}
    for row in rows:  # ordered desc within each unit: first wins
        latest.setdefault(row.unit, row.price_usd)
    required = (PriceUnit.PER_1M_INPUT, PriceUnit.PER_1M_CACHED_INPUT, PriceUnit.PER_1M_OUTPUT)
    missing = [unit.value for unit in required if unit not in latest]
    if missing:
        msg = f"no price for model {model!r}, units: {', '.join(missing)}"
        raise PricingError(msg)
    return PriceSheet(
        model=model,
        input_usd=latest[PriceUnit.PER_1M_INPUT],
        cached_input_usd=latest[PriceUnit.PER_1M_CACHED_INPUT],
        output_usd=latest[PriceUnit.PER_1M_OUTPUT],
    )


async def load_embedding_price_usd(
    db: AsyncSession, model: str, at: datetime | None = None
) -> Decimal:
    """USD per 1M INPUT tokens for an embedding model.

    An embedding model has no cached-input or output price, so asking
    `load_price_sheet` for it always failed — and every RAG query and
    every indexing run was then booked at zero cost. Fails closed like
    the other loaders when even the input price is missing.
    """
    moment = at or datetime.now(UTC)
    price = (
        await db.execute(
            select(ModelPrice.price_usd)
            .where(
                ModelPrice.model == model,
                ModelPrice.unit == PriceUnit.PER_1M_INPUT,
                ModelPrice.effective_from <= moment,
            )
            .order_by(ModelPrice.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if price is None:
        msg = f"no per-1M-input price for embedding model {model!r} (set it in Лимиты)"
        raise PricingError(msg)
    return price


def compute_embedding_cost_usd(price_per_1m: Decimal, tokens: int) -> Decimal:
    return (Decimal(tokens) * price_per_1m / _MILLION).quantize(Decimal("0.000001"))


async def load_audio_price_usd(db: AsyncSession, model: str, at: datetime | None = None) -> Decimal:
    """USD per MINUTE of audio for the transcription model.

    Fails closed like the token prices: without a price, transcription
    does not happen at all rather than running unpriced.
    """
    moment = at or datetime.now(UTC)
    price = (
        await db.execute(
            select(ModelPrice.price_usd)
            .where(
                ModelPrice.model == model,
                ModelPrice.unit == PriceUnit.PER_MINUTE_AUDIO,
                ModelPrice.effective_from <= moment,
            )
            .order_by(ModelPrice.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if price is None:
        msg = f"no per-minute audio price for model {model!r} (set it in Лимиты)"
        raise PricingError(msg)
    return price


def compute_audio_cost_usd(price_per_minute: Decimal, seconds: float) -> Decimal:
    """Billed by duration, so seconds are converted, not rounded up to a
    whole minute — a 12-second voice message costs 12 seconds."""
    minutes = Decimal(str(max(seconds, 0.0))) / Decimal(60)
    return (price_per_minute * minutes).quantize(_USD_PLACES)


async def load_usd_kzt_rate(db: AsyncSession, at: datetime | None = None) -> Decimal:
    """Owner-entered rate effective at the moment; PricingError if unset."""
    moment = at or datetime.now(UTC)
    rate = (
        await db.execute(
            select(FxRate.rate)
            .where(FxRate.pair == USD_KZT, FxRate.effective_from <= moment)
            .order_by(FxRate.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if rate is None:
        msg = "USD/KZT rate is not configured (set it in the admin, Лимиты)"
        raise PricingError(msg)
    return rate


def compute_cost_usd(prices: PriceSheet, usage: Usage) -> Decimal:
    """SPEC §9.1: cached input is billed at its own (cheaper) rate;
    reasoning tokens are billed as output."""
    plain_input = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cost = (
        Decimal(plain_input) * prices.input_usd
        + Decimal(usage.cached_input_tokens) * prices.cached_input_usd
        + Decimal(usage.output_tokens + usage.reasoning_tokens) * prices.output_usd
    ) / _MILLION
    return cost.quantize(_USD_PLACES)


def usd_to_kzt(amount_usd: Decimal, rate: Decimal) -> Decimal:
    return (amount_usd * rate).quantize(_KZT_PLACES)


def kzt_to_usd(amount_kzt: Decimal, rate: Decimal) -> Decimal:
    return (amount_kzt / rate).quantize(Decimal("0.000001"))


def estimate_max_cost_usd(
    prices: PriceSheet, max_input_tokens: int, max_output_tokens: int
) -> Decimal:
    """Worst-case cost used for the reservation (never for accounting):
    full uncached input plus full output at list prices."""
    cost = (
        Decimal(max_input_tokens) * prices.input_usd
        + Decimal(max_output_tokens) * prices.output_usd
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))


async def seed_default_prices(db: AsyncSession, settings: Settings) -> list[str]:
    """SPEC §9.1 starting prices, materialized ONLY by the owner-run
    `make seed-prices` — never silently by a migration. Model names come
    from settings; a model+unit that already has any price is skipped so
    the command never overrides what the owner manages in the admin."""
    entries = (
        (settings.openai_chat_model, PriceUnit.PER_1M_INPUT, Decimal("0.20")),
        (settings.openai_chat_model, PriceUnit.PER_1M_CACHED_INPUT, Decimal("0.02")),
        (settings.openai_chat_model, PriceUnit.PER_1M_OUTPUT, Decimal("1.20")),
        (settings.openai_embedding_model, PriceUnit.PER_1M_INPUT, Decimal("0.02")),
        (settings.openai_transcribe_model, PriceUnit.PER_MINUTE_AUDIO, Decimal("0.0045")),
    )
    now = datetime.now(UTC)
    created: list[str] = []
    for model, unit, price in entries:
        exists = (
            await db.execute(
                select(ModelPrice.id)
                .where(ModelPrice.model == model, ModelPrice.unit == unit)
                .limit(1)
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(ModelPrice(model=model, unit=unit, price_usd=price, effective_from=now))
        created.append(f"{model} {unit.value} = ${price}")
    return created
