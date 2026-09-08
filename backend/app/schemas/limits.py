"""Schemas for the Лимиты section: settings, prices, fx, spend snapshot."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class LimitsSettings(BaseModel):
    """SPEC §9.3 knobs, validated ranges. All money fields are ₸."""

    max_input_tokens: int = Field(ge=1_000, le=50_000)
    max_output_tokens: int = Field(ge=50, le=2_000)
    max_ai_calls_per_message: int = Field(ge=1, le=5)
    max_ai_cost_per_dialog_kzt: float = Field(ge=0, le=1_000_000)
    max_ai_cost_per_user_per_day_kzt: float = Field(ge=0, le=1_000_000)
    daily_ai_budget_kzt: float = Field(ge=0, le=10_000_000)
    monthly_ai_budget_kzt: float = Field(ge=0, le=100_000_000)
    debounce_seconds: int = Field(ge=0, le=30)
    max_messages_per_user_per_minute: int = Field(ge=1, le=60)
    max_messages_per_user_per_hour: int = Field(ge=1, le=1_000)
    spam_repeat_threshold: int = Field(ge=2, le=20)
    ai_max_message_age_minutes: int = Field(ge=5, le=1_440)
    circuit_breaker_failures: int = Field(ge=1, le=50)
    circuit_breaker_cooldown_minutes: int = Field(ge=1, le=120)
    semantic_cache_enabled: bool
    semantic_cache_ttl_hours: int = Field(ge=1, le=720)
    allowed_link_domains: list[str] = Field(default_factory=list, max_length=20)
    # --- media (phase 7) ---
    transcription_enabled: bool
    vision_enabled: bool
    max_audio_seconds: int = Field(ge=5, le=600)
    max_audio_file_mb: int = Field(ge=1, le=64)
    max_images_per_dialog: int = Field(ge=0, le=50)
    max_image_file_mb: int = Field(ge=1, le=32)
    media_retention_days: int = Field(ge=1, le=365)
    transcription_terms: str = Field(default="", max_length=1000)


class PriceItem(BaseModel):
    model: str
    unit: str
    price_usd: Decimal
    effective_from: datetime


class LedgerSnapshot(BaseModel):
    scope: str
    period: str
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal


class LimitsResponse(BaseModel):
    settings: LimitsSettings
    prices: list[PriceItem]
    usd_kzt_rate: Decimal | None
    ledgers: list[LedgerSnapshot]
    dry_run: bool
    # Explicit reminder rendered in the UI: DRY_RUN отключает только
    # отправку сообщений — расходы на OpenAI идут по-настоящему.


class LimitsUpdateRequest(BaseModel):
    settings: LimitsSettings


class FxRateRequest(BaseModel):
    rate: Decimal = Field(gt=0, le=100_000)


class PriceRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    unit: str = Field(pattern="^(per_1m_input|per_1m_cached_input|per_1m_output|per_minute_audio)$")
    price_usd: Decimal = Field(ge=0, le=1_000)


class OkResponse(BaseModel):
    ok: bool = True
    detail: dict[str, Any] | None = None
