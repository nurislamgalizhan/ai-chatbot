"""Agent instruction versions: create, activate, fetch active.

Exactly one active version is guaranteed by a partial unique index; the
activation switch happens in one transaction so a race of two activations
is resolved by the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from app.models import AgentInstruction
from app.security.audit import record_audit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_active_instruction(db: AsyncSession) -> AgentInstruction | None:
    return (
        await db.execute(select(AgentInstruction).where(AgentInstruction.is_active.is_(True)))
    ).scalar_one_or_none()


async def create_version(
    db: AsyncSession,
    *,
    system_prompt: str,
    style: str | None,
    escalation_rules: dict[str, Any] | None,
    max_output_tokens: int | None,
    reasoning_effort: str | None,
    author_user_id: int | None,
    correlation_id: str | None = None,
) -> AgentInstruction:
    """A new version is never active on creation: the owner activates it
    explicitly (ideally after a sandbox run)."""
    next_version = (
        await db.execute(select(func.coalesce(func.max(AgentInstruction.version), 0) + 1))
    ).scalar_one()
    instruction = AgentInstruction(
        version=next_version,
        system_prompt=system_prompt,
        style=style,
        fallback_templates={},
        escalation_rules=escalation_rules,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        author_user_id=author_user_id,
    )
    db.add(instruction)
    await db.flush()
    await record_audit(
        db,
        action="agent.instruction_created",
        actor_user_id=author_user_id,
        entity_type="agent_instruction",
        entity_id=str(instruction.id),
        after={"version": instruction.version},
        correlation_id=correlation_id,
    )
    return instruction


async def activate_version(
    db: AsyncSession,
    instruction: AgentInstruction,
    *,
    actor_user_id: int | None,
    correlation_id: str | None = None,
) -> None:
    """Deactivate-then-activate in one transaction; the partial unique
    index turns a concurrent double-activation into a constraint error
    instead of two active versions."""
    await db.execute(
        update(AgentInstruction).where(AgentInstruction.is_active.is_(True)).values(is_active=False)
    )
    instruction.is_active = True
    await db.flush()
    await record_audit(
        db,
        action="agent.instruction_activated",
        actor_user_id=actor_user_id,
        entity_type="agent_instruction",
        entity_id=str(instruction.id),
        after={"version": instruction.version},
        correlation_id=correlation_id,
    )
