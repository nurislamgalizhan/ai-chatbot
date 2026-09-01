"""Dialog mode state machine (SPEC §7).

Rules enforced here:
- every FROM->TO transition lands in chat_mode_history + audit_log;
- a timer extension only moves paused_until (no history spam);
- DISABLED never changes automatically — only a human flips it;
- the return from PAUSED_UNTIL is done by an ACTIVE scheduler, never a
  lazy check, so the mode shown in the chat list never lies;
- the global emergency switch mutes AI everywhere WITHOUT touching
  per-chat modes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from app.models import Chat, ChatModeHistory
from app.models.enums import ChatMode, MsgSource
from app.security.audit import record_audit
from app.services.app_settings import (
    OPERATOR_PAUSE_EXTEND,
    OPERATOR_PAUSE_MINUTES,
    PANEL_REPLY_MODE,
    get_setting,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

_SCHEDULER_BATCH = 100


def _now() -> datetime:
    return datetime.now(UTC)


def ai_allowed(chat: Chat, *, emergency_off: bool, now: datetime | None = None) -> bool:
    """THE gate the (phase 6) pipeline consults before any AI work.

    PAUSED_UNTIL is False even when the timer has expired: the scheduler
    is the only thing that flips modes back (active return, SPEC §7).
    """
    if emergency_off:
        return False
    return chat.mode is ChatMode.AUTO_AI


async def set_mode(
    db: AsyncSession,
    chat: Chat,
    to_mode: ChatMode,
    *,
    changed_by: str,
    reason: str,
    paused_until: datetime | None = None,
    actor_user_id: int | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Transition the chat mode; returns False when it is a no-op."""
    if chat.mode is to_mode and chat.paused_until == paused_until:
        return False
    from_mode = chat.mode
    chat.mode = to_mode
    chat.paused_until = paused_until if to_mode is ChatMode.PAUSED_UNTIL else None
    chat.mode_changed_by = changed_by
    chat.mode_changed_at = _now()
    chat.mode_change_reason = reason
    db.add(
        ChatModeHistory(
            chat_id=chat.id,
            from_mode=from_mode,
            to_mode=to_mode,
            actor_user_id=actor_user_id,
            reason=reason,
            paused_until=chat.paused_until,
        )
    )
    await record_audit(
        db,
        action="chat.mode_changed",
        actor_user_id=actor_user_id,
        entity_type="chat",
        entity_id=str(chat.id),
        before={"mode": from_mode.value},
        after={
            "mode": to_mode.value,
            "paused_until": chat.paused_until.isoformat() if chat.paused_until else None,
            "changed_by": changed_by,
            "reason": reason,
        },
        correlation_id=correlation_id,
    )
    return True


async def on_operator_message(
    db: AsyncSession,
    chat: Chat,
    *,
    source: MsgSource,
    correlation_id: str | None = None,
) -> None:
    """A human replied (panel or phone): mute/pause AI per settings.

    - DISABLED is never overridden automatically;
    - HUMAN stays HUMAN (already manually muted, nothing to do);
    - panel replies follow PANEL_REPLY_MODE (HUMAN or timed pause);
    - phone replies always use the timed pause;
    - an already-running pause is extended if the setting says so.
    """
    if chat.mode in (ChatMode.DISABLED, ChatMode.HUMAN):
        return

    changed_by = "operator_panel" if source is MsgSource.OPERATOR_PANEL else "operator_phone"
    panel_mode = await get_setting(db, PANEL_REPLY_MODE)
    if source is MsgSource.OPERATOR_PANEL and panel_mode == "HUMAN":
        await set_mode(
            db,
            chat,
            ChatMode.HUMAN,
            changed_by=changed_by,
            reason="operator_replied",
            correlation_id=correlation_id,
        )
        return

    minutes = int(await get_setting(db, OPERATOR_PAUSE_MINUTES))
    new_until = _now() + timedelta(minutes=minutes)
    if chat.mode is ChatMode.PAUSED_UNTIL:
        extend = bool(await get_setting(db, OPERATOR_PAUSE_EXTEND))
        if extend and (chat.paused_until is None or new_until > chat.paused_until):
            # Extension, not a transition: no history row on purpose.
            chat.paused_until = new_until
        return
    await set_mode(
        db,
        chat,
        ChatMode.PAUSED_UNTIL,
        changed_by=changed_by,
        reason="operator_replied",
        paused_until=new_until,
        correlation_id=correlation_id,
    )


async def resume_expired_pauses(db: AsyncSession) -> int:
    """Active scheduler: PAUSED_UNTIL past its deadline -> AUTO_AI."""
    chats = (
        (
            await db.execute(
                select(Chat)
                .where(Chat.mode == ChatMode.PAUSED_UNTIL, Chat.paused_until <= _now())
                .limit(_SCHEDULER_BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for chat in chats:
        await set_mode(
            db,
            chat,
            ChatMode.AUTO_AI,
            changed_by="scheduler",
            reason="pause_expired",
        )
        logger.info("chat_pause_expired", chat_id=chat.id)
    return len(chats)
