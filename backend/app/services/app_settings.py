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

DEFAULTS: dict[str, Any] = {
    EMERGENCY_AI_OFF: False,
    PANEL_REPLY_MODE: "PAUSED_UNTIL",
    OPERATOR_PAUSE_MINUTES: 60,
    OPERATOR_PAUSE_EXTEND: True,
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
