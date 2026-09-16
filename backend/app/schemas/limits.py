"""Schemas for the Лимиты section: settings, prices, fx, spend snapshot."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LimitsSettings(BaseModel):
    """SPEC §9.3 knobs, validated ranges. All money fields are ₸."""

    # Doubles as the request body of PUT /api/limits: a `spent_usd` or
    # `password_hash` smuggled in here is rejected, not dropped.
    model_config = ConfigDict(extra="forbid")

    max_input_tokens: int = Field(ge=1_000, le=50_000)
    max_output_tokens: int = Field(ge=50, le=2_000)
    max_ai_calls_per_message: int = Field(ge=1, le=5)
    max_ai_cost_per_dialog_kzt: float = Field(ge=0, le=1_000_000)
    max_ai_cost_per_user_per_day_kzt: float = Field(ge=0, le=1_000_000)
    daily_ai_budget_kzt: float = Field(ge=0, le=10_000_000)
    monthly_ai_budget_kzt: float = Field(ge=0, le=100_000_000)
    # 0 = отвечать на первое же сообщение; верх — как у прошлого вендора.
    debounce_seconds: int = Field(ge=0, le=60)
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
    media_reply_cooldown_minutes: int = Field(ge=0, le=180)
    # --- alerts and retention (phase 9) ---
    #: Empty until the owner opts in: while it is empty nothing is ever
    #: sent to any number, alerts only accumulate in «Система».
    alert_phone: str = Field(default="", max_length=20)
    alerts_enabled: bool = True
    disk_alert_percent: int = Field(ge=50, le=99)
    queue_alert_depth: int = Field(ge=1, le=10_000)
    backup_stale_hours: int = Field(ge=6, le=720)
    retention_raw_payload_days: int = Field(ge=1, le=365)
    retention_system_events_days: int = Field(ge=7, le=1_095)
    retention_faq_misses_days: int = Field(ge=7, le=1_095)
    retention_audit_log_days: int = Field(ge=30, le=3_650)
    retention_transport_days: int = Field(ge=1, le=365)
    # --- provider features (GREEN-API) ---
    #: 0 = off; otherwise WhatsApp's own 1..20 s window. Free (a field of
    #: sendMessage), but it delays the answer by this much.
    typing_time_ms: int = Field(ge=0, le=20_000)
    link_preview_enabled: bool
    quote_customer_message: bool
    provider_queue_alert_depth: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def _debounce_fits_the_stale_window(self) -> LimitsSettings:
        """Two settings, each valid alone, can contradict each other.

        Messages older than `ai_max_message_age_minutes` are never
        answered. The debounce spends part of that window before the bot
        even starts, so with a 5-minute cutoff a 60-second pause would
        burn a fifth of it — and a customer writing every 50 seconds
        could have his message expire while the bot politely waits.
        """
        budget = self.ai_max_message_age_minutes * 60
        if self.debounce_seconds * 10 > budget:
            msg = (
                "Задержка перед ответом слишком велика для текущей отсечки возраста "
                f"({self.ai_max_message_age_minutes} мин): максимум {budget // 10} сек"
            )
            raise ValueError(msg)
        return self


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
    #: Set after saving an alert phone that has no WhatsApp: a warning,
    #: not an error — the check itself can fail for its own reasons, and
    #: refusing to save the owner's own number would be worse.
    notice: str | None = None
    # Explicit reminder rendered in the UI: DRY_RUN отключает только
    # отправку сообщений — расходы на OpenAI идут по-настоящему.


class LimitsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: LimitsSettings


class FxRateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: Decimal = Field(gt=0, le=100_000)


class PriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    unit: str = Field(pattern="^(per_1m_input|per_1m_cached_input|per_1m_output|per_minute_audio)$")
    price_usd: Decimal = Field(ge=0, le=1_000)


class OkResponse(BaseModel):
    ok: bool = True
    detail: dict[str, Any] | None = None
