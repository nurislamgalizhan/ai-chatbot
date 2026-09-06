"""Structured per-chat memory, updated by cheap rules — never by a full
LLM pass per message (SPEC §8). The summary micro-call is deliberately
deferred (phase 6 gate report records this): the context builder reads
summary_text when present, but nothing generates it yet.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import ChatMemory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.enums import KbLanguage

_NAME_RES = (
    re.compile(r"меня зовут\s+([А-ЯЁӘҒҚҢӨҰҮІҺ][\w-]{1,30})", re.IGNORECASE),
    re.compile(r"менің атым\s+([А-ЯЁӘҒҚҢӨҰҮІҺ][\w-]{1,30})", re.IGNORECASE),
    re.compile(r"my name is\s+([A-Z][\w-]{1,30})", re.IGNORECASE),
)
_DATE_RE = re.compile(
    r"\b(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|сегодня|завтра|послезавтра|бүгін|ертең)\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")


def extract_updates(text: str) -> dict[str, str]:
    """Regex-only extraction; unmatched fields are simply not updated."""
    updates: dict[str, str] = {}
    for name_re in _NAME_RES:
        match = name_re.search(text)
        if match:
            updates["client_name"] = match.group(1)
            break
    date_match = _DATE_RE.search(text)
    if date_match:
        updates["requested_date"] = date_match.group(1).lower()
    time_match = _TIME_RE.search(text)
    if time_match:
        updates["requested_time"] = time_match.group(1)
    return updates


async def remember(
    db: AsyncSession,
    chat_id: int,
    *,
    text: str,
    language: KbLanguage,
    interest: str | None = None,
) -> None:
    """Upsert the single memory row for the chat."""
    updates = extract_updates(text)
    updates["language"] = language.value
    if interest:
        updates["interest"] = interest
    stmt = (
        pg_insert(ChatMemory)
        .values(chat_id=chat_id, **updates)
        .on_conflict_do_update(index_elements=["chat_id"], set_=updates)
    )
    await db.execute(stmt)


def snapshot(memory: ChatMemory | None) -> dict[str, Any] | None:
    """Compact JSON block for the prompt: only the fields that are set."""
    if memory is None:
        return None
    fields = {
        "language": memory.language,
        "interest": memory.interest,
        "requested_date": memory.requested_date,
        "requested_time": memory.requested_time,
        "client_name": memory.client_name,
        "handoff_required": memory.handoff_required or None,
        "last_topic": memory.last_topic,
    }
    compact = {key: value for key, value in fields.items() if value}
    return compact or None
