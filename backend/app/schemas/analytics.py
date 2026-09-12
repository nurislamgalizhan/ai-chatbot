"""Response shapes for the Обзор / Расходы / Аналитика sections."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class PeriodInfo(BaseModel):
    start: date
    end: date
    days: int
    #: Stated out loud: day boundaries are UTC, the same ones the budget
    #: periods use, so two pages cannot disagree about "today".
    timezone: str = "UTC"


class DaySpend(BaseModel):
    day: date
    by_call_type: dict[str, str]
    total_usd: str
    total_kzt: str


class DayTokens(BaseModel):
    day: date
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    audio_seconds: int
    cost_usd: str
    cost_kzt: str


class CostsResponse(BaseModel):
    period: PeriodInfo
    labels: dict[str, str]
    spend_by_day: list[DaySpend]
    tokens_by_day: list[DayTokens]
    by_model: list[dict[str, object]]
    top_dialogs: list[dict[str, object]]
    budget: dict[str, object]
    total_usd: str
    total_kzt: str


class AnalyticsResponse(BaseModel):
    period: PeriodInfo
    workload: dict[str, object]
    dialogs: dict[str, object]
    answer_sources: dict[str, int]
    errors: list[dict[str, object]]
    language_gaps: list[dict[str, object]]
    #: Off by default (owner decision, phase 6): without this the донат
    #: simply has no «повтор из кэша» slice and looks like a bug.
    semantic_cache_enabled: bool = False
    #: Metrics SPEC asks for that this schema cannot support honestly.
    #: Shown in the UI as a short note instead of a fabricated chart.
    not_measured: list[str] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    queue_depth: int
    needs_attention: int
    unread_chats: int
    failures_24h: int
    spend_today_kzt: str
    spend_month_kzt: str
    today: dict[str, object]
    budget: dict[str, object]
    dry_run: bool
    ai_enabled: bool
