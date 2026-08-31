from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    actor_user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append an audit record. Callers must never pass secrets or customer
    message texts in before/after."""
    db.add(
        AuditLog(
            action=action,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            ip=ip,
            correlation_id=correlation_id,
        )
    )
    await db.flush()
