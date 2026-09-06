"""Typed access to runtime settings. Defaults live HERE, in code — the
settings table stores only values the owner explicitly changed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import AppSetting
from app.security.audit import record_audit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# --- phase 4 keys ---
# True = AI is globally muted in every chat without touching per-chat modes.
EMERGENCY_AI_OFF = "emergency_ai_off"
# What an operator reply from the PANEL does to the chat mode:
# "PAUSED_UNTIL" (default, mirrors the owner's Savvi config) or "HUMAN".
PANEL_REPLY_MODE = "panel_reply_mode"
# Pause length for operator interruptions (owner's Savvi config: 60 min).
OPERATOR_PAUSE_MINUTES = "operator_pause_minutes"
# Every new operator message extends the running pause timer.
OPERATOR_PAUSE_EXTEND = "operator_pause_extend"

# --- phase 6 keys: context and output caps (SPEC §9.3) ---
MAX_INPUT_TOKENS = "max_input_tokens"
MAX_OUTPUT_TOKENS = "max_output_tokens"
MAX_AI_CALLS_PER_MESSAGE = "max_ai_calls_per_message"
# --- phase 6 keys: money (limits are entered in ₸, SPEC §9.3) ---
MAX_AI_COST_PER_DIALOG_KZT = "max_ai_cost_per_dialog_kzt"
MAX_AI_COST_PER_USER_PER_DAY_KZT = "max_ai_cost_per_user_per_day_kzt"
DAILY_AI_BUDGET_KZT = "daily_ai_budget_kzt"
MONTHLY_AI_BUDGET_KZT = "monthly_ai_budget_kzt"
# --- phase 6 keys: pipeline behaviour ---
DEBOUNCE_SECONDS = "debounce_seconds"
MAX_MESSAGES_PER_USER_PER_MINUTE = "max_messages_per_user_per_minute"
MAX_MESSAGES_PER_USER_PER_HOUR = "max_messages_per_user_per_hour"
SPAM_REPEAT_THRESHOLD = "spam_repeat_threshold"
# Messages older than this are never answered by AI (owner decision
# 2026-09-06: 1 hour) — they get IGNORED + needs_attention instead.
AI_MAX_MESSAGE_AGE_MINUTES = "ai_max_message_age_minutes"
CIRCUIT_BREAKER_FAILURES = "circuit_breaker_failures"
CIRCUIT_BREAKER_COOLDOWN_MINUTES = "circuit_breaker_cooldown_minutes"
# Domains the model may link to in answers; anything else fails output
# validation and the reply is replaced with the handoff template.
ALLOWED_LINK_DOMAINS = "allowed_link_domains"
# --- phase 6 keys: semantic cache ---
# Off by default (owner-visible risk/benefit tradeoff: stale answers vs
# pennies saved at current traffic).
SEMANTIC_CACHE_ENABLED = "semantic_cache_enabled"
SEMANTIC_CACHE_TTL_HOURS = "semantic_cache_ttl_hours"
# Monotonic counter bumped by EVERY kb_documents/kb_faq mutation; part of
# the cache key, so bumping invalidates the whole cache without a scan.
KB_VERSION = "kb_version"

DEFAULTS: dict[str, Any] = {
    EMERGENCY_AI_OFF: False,
    PANEL_REPLY_MODE: "PAUSED_UNTIL",
    OPERATOR_PAUSE_MINUTES: 60,
    OPERATOR_PAUSE_EXTEND: True,
    MAX_INPUT_TOKENS: 12000,
    MAX_OUTPUT_TOKENS: 350,
    MAX_AI_CALLS_PER_MESSAGE: 2,
    MAX_AI_COST_PER_DIALOG_KZT: 20,
    MAX_AI_COST_PER_USER_PER_DAY_KZT: 50,
    DAILY_AI_BUDGET_KZT: 300,
    MONTHLY_AI_BUDGET_KZT: 3000,
    DEBOUNCE_SECONDS: 3,
    MAX_MESSAGES_PER_USER_PER_MINUTE: 5,
    MAX_MESSAGES_PER_USER_PER_HOUR: 30,
    SPAM_REPEAT_THRESHOLD: 5,
    AI_MAX_MESSAGE_AGE_MINUTES: 60,
    CIRCUIT_BREAKER_FAILURES: 5,
    CIRCUIT_BREAKER_COOLDOWN_MINUTES: 10,
    # Empty = the model may not link anywhere; the owner allowlists
    # domains (e.g. a maps service) in the admin. FAQ answers are owner
    # text and are not subject to this check.
    ALLOWED_LINK_DOMAINS: [],
    SEMANTIC_CACHE_ENABLED: False,
    SEMANTIC_CACHE_TTL_HOURS: 24,
    KB_VERSION: 0,
}


async def get_setting(db: AsyncSession, key: str) -> Any:  # noqa: ANN401 - JSON values
    row = await db.get(AppSetting, key)
    if row is not None:
        return row.value
    return DEFAULTS[key]


async def set_setting(
    db: AsyncSession,
    key: str,
    value: Any,  # noqa: ANN401 - JSON values
    *,
    actor_user_id: int | None = None,
    correlation_id: str | None = None,
) -> None:
    previous = await get_setting(db, key)
    stmt = (
        pg_insert(AppSetting)
        .values(key=key, value=value, value_type=type(value).__name__, updated_by=actor_user_id)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value": value,
                "value_type": type(value).__name__,
                "updated_by": actor_user_id,
            },
        )
    )
    await db.execute(stmt)
    await record_audit(
        db,
        action="settings.changed",
        actor_user_id=actor_user_id,
        entity_type="setting",
        entity_id=key,
        before={"value": previous},
        after={"value": value},
        correlation_id=correlation_id,
    )
