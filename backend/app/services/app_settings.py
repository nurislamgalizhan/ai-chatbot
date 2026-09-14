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

# --- phase 7 keys: media (SPEC §13, §9.3) ---
# Voice: both caps are checked BEFORE the paid transcription — the file
# size before downloading, the duration locally after (mutagen).
MAX_AUDIO_SECONDS = "max_audio_seconds"
MAX_AUDIO_FILE_MB = "max_audio_file_mb"
# Images: per-dialog cap stops a picture flood from draining the budget;
# the file cap is checked before Pillow ever decodes the bytes.
MAX_IMAGES_PER_DIALOG = "max_images_per_dialog"
MAX_IMAGE_FILE_MB = "max_image_file_mb"
# Files are deleted after this many days; transcripts and image
# descriptions stay forever (SPEC §13 retention).
MEDIA_RETENTION_DAYS = "media_retention_days"
# Kill switches: with either off, that media type is handed to a human
# instead of costing money.
TRANSCRIPTION_ENABLED = "transcription_enabled"
VISION_ENABLED = "vision_enabled"
# Domain terms passed to the transcription model so it spells the org
# name, section and place names correctly. White-label: owner-supplied,
# nothing company-specific in code.
TRANSCRIPTION_TERMS = "transcription_terms"
# How long after telling a customer "your file went to a manager" the bot
# stays silent about further files in the same chat. Composing that
# sentence is a PAID model call (no canned templates since 2026-09-11),
# and the media path has no per-customer rate limit of its own: without
# this, twenty photos in a row bought twenty refusals.
MEDIA_REPLY_COOLDOWN_MINUTES = "media_reply_cooldown_minutes"

# --- phase 9 keys: retention (docs/security-and-cost-controls.md §5) ---
# The customer's own words inside the provider payload. Wiped first and
# soonest: after 30 days the message itself is in `messages`, and the
# JSONB copy is only a second place for the same personal data to leak.
RETENTION_RAW_PAYLOAD_DAYS = "retention_raw_payload_days"
RETENTION_SYSTEM_EVENTS_DAYS = "retention_system_events_days"
RETENTION_FAQ_MISSES_DAYS = "retention_faq_misses_days"
RETENTION_AUDIT_LOG_DAYS = "retention_audit_log_days"
# Delivery attempts and finished media jobs behind the visible history.
RETENTION_TRANSPORT_DAYS = "retention_transport_days"

# --- provider features (GREEN-API) ---
# "Печатает…" before the answer. Free: it is a field of sendMessage, not
# a second request (SPEC §16 allowed the indicator on exactly that
# condition). It DELAYS delivery by this much, so it is the owner's call.
# 0 = off.
TYPING_TIME_MS = "typing_time_ms"
# Link previews in bot answers. Off by default: a link in a two-line
# answer pulls up to 2 MB of preview and pushes the text out of view.
LINK_PREVIEW_ENABLED = "link_preview_enabled"
# Answer quoting the customer's own message. Helps in chats where a
# manager writes from the phone at the same time as the bot.
QUOTE_CUSTOMER_MESSAGE = "quote_customer_message"
# Provider-side queue depth that means our polling is not keeping up.
PROVIDER_QUEUE_ALERT_DEPTH = "provider_queue_alert_depth"

# --- phase 9 keys: alerts and monitoring ---
# WhatsApp number that receives alerts. Empty = nothing is ever sent
# anywhere (the owner has not opted in yet); alerts still accumulate and
# are visible in «Система».
ALERT_PHONE = "alert_phone"
ALERTS_ENABLED = "alerts_enabled"
# Disk fill level that makes the worker alert the owner (SPEC: >85%).
DISK_ALERT_PERCENT = "disk_alert_percent"
# Queue depth that means the bot is falling behind, not merely busy.
QUEUE_ALERT_DEPTH = "queue_alert_depth"
# Hours without a successful database dump before the owner is told.
BACKUP_STALE_HOURS = "backup_stale_hours"

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
    MAX_AUDIO_SECONDS: 120,
    MAX_AUDIO_FILE_MB: 16,
    MAX_IMAGES_PER_DIALOG: 3,
    MAX_IMAGE_FILE_MB: 10,
    MEDIA_RETENTION_DAYS: 7,
    TRANSCRIPTION_ENABLED: True,
    # OFF by default, deliberately. Voice notes are the customer's actual
    # question; photos are where the risk is: an unlabelled photo of a
    # passport or bank card cannot be recognized as sensitive without
    # sending it somewhere, and SPEC §13 forbids exactly that. The owner
    # turns it on knowingly in «Лимиты» → «Медиа».
    VISION_ENABLED: False,
    TRANSCRIPTION_TERMS: "",
    MEDIA_REPLY_COOLDOWN_MINUTES: 10,
    RETENTION_RAW_PAYLOAD_DAYS: 30,
    RETENTION_SYSTEM_EVENTS_DAYS: 90,
    RETENTION_FAQ_MISSES_DAYS: 180,
    RETENTION_AUDIT_LOG_DAYS: 365,
    RETENTION_TRANSPORT_DAYS: 30,
    TYPING_TIME_MS: 1500,
    LINK_PREVIEW_ENABLED: False,
    QUOTE_CUSTOMER_MESSAGE: True,
    PROVIDER_QUEUE_ALERT_DEPTH: 20,
    ALERT_PHONE: "",
    ALERTS_ENABLED: True,
    DISK_ALERT_PERCENT: 85,
    QUEUE_ALERT_DEPTH: 50,
    BACKUP_STALE_HOURS: 36,
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
