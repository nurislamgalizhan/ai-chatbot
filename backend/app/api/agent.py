"""Agent instruction editor + sandbox (SPEC §12.3).

The sandbox runs the SAME generate_reply seam the worker uses — real
retrieval, real budget reservation (global scopes; there is no chat),
real model call, and the spend lands in ai_calls with chat_id NULL:
sandbox money is never unaccounted. Nothing touches chats or the outbox.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser, AgentInstruction
from app.schemas.agent import (
    InstructionItem,
    InstructionListResponse,
    InstructionPayload,
    SandboxRequest,
    SandboxResponse,
    SandboxUsage,
)
from app.security import rate_limit
from app.security.deps import get_app_settings, get_db, get_redis, require_admin
from app.services.agent import activate_version, create_version, get_active_instruction
from app.services.generation import (
    GenerationDeps,
    GenerationInput,
    generate_reply,
    new_sandbox_correlation,
)
from app.services.kb_text import detect_language

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(require_admin)])

INSTRUCTION_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Версия инструкции не найдена"
)

# The sandbox spends real money: keep a hard per-minute cap on top of the
# budget reservation it already goes through.
_SANDBOX_RATE_LIMIT = 10
_SANDBOX_RATE_WINDOW_SECONDS = 60


def _item(row: AgentInstruction) -> InstructionItem:
    return InstructionItem(
        id=row.id,
        version=row.version,
        is_active=row.is_active,
        system_prompt=row.system_prompt,
        style=row.style,
        escalation_rules=row.escalation_rules,
        max_output_tokens=row.max_output_tokens,
        reasoning_effort=row.reasoning_effort,
        created_at=row.created_at,
    )


@router.get("/instructions", response_model=InstructionListResponse)
async def list_instructions(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InstructionListResponse:
    rows = (
        (await db.execute(select(AgentInstruction).order_by(AgentInstruction.version.desc())))
        .scalars()
        .all()
    )
    active = next((r.version for r in rows if r.is_active), None)
    return InstructionListResponse(items=[_item(r) for r in rows], active_version=active)


@router.post("/instructions", response_model=InstructionItem)
async def create_instruction(
    body: InstructionPayload,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InstructionItem:
    """Creates an INACTIVE version; activation is a separate, explicit
    action (ideally after a sandbox run)."""
    instruction = await create_version(
        db,
        system_prompt=body.system_prompt.strip(),
        style=(body.style or "").strip() or None,
        escalation_rules=body.escalation_rules,
        max_output_tokens=body.max_output_tokens,
        reasoning_effort=body.reasoning_effort,
        author_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return _item(instruction)


@router.post("/instructions/{instruction_id}/activate", response_model=InstructionItem)
async def activate_instruction(
    instruction_id: int,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InstructionItem:
    """Activating an OLD version IS the rollback mechanism."""
    instruction = await db.get(AgentInstruction, instruction_id)
    if instruction is None:
        raise INSTRUCTION_NOT_FOUND
    if not instruction.is_active:
        await activate_version(
            db,
            instruction,
            actor_user_id=user.id,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    return _item(instruction)


@router.post("/sandbox", response_model=SandboxResponse)
async def sandbox_run(
    body: SandboxRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> SandboxResponse:
    allowed = await rate_limit.allow(
        redis, "rl:agent:sandbox", _SANDBOX_RATE_LIMIT, _SANDBOX_RATE_WINDOW_SECONDS
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много запросов к песочнице — подождите минуту",
        )
    if await get_active_instruction(db) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Сначала создайте и активируйте версию инструкции",
        )
    chat_provider = getattr(request.app.state, "chat_provider", None)
    embedding_provider = getattr(request.app.state, "embedding_provider", None)
    if chat_provider is None or embedding_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Песочница недоступна: не настроен ключ OpenAI",
        )
    settings = get_app_settings(request)
    deps = GenerationDeps(
        sessionmaker=request.app.state.sessionmaker,
        settings=settings,
        redis=redis,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
    )
    language = detect_language(body.question)
    outcome = await generate_reply(
        deps,
        GenerationInput(
            correlation_id=new_sandbox_correlation(),
            question=body.question,
            language=language,
        ),
    )
    usage = (
        SandboxUsage(
            input_tokens=outcome.usage.input_tokens,
            cached_input_tokens=outcome.usage.cached_input_tokens,
            output_tokens=outcome.usage.output_tokens,
            reasoning_tokens=outcome.usage.reasoning_tokens,
        )
        if outcome.usage
        else None
    )
    return SandboxResponse(
        kind=outcome.kind,
        text=outcome.text,
        error_code=outcome.error_code,
        cost_usd=str(outcome.cost_usd),
        cost_kzt=str(outcome.cost_kzt),
        usage=usage,
        latency_ms=outcome.latency_ms,
        kb_chunk_ids=outcome.kb_chunk_ids,
        instruction_version=outcome.instruction_version,
        model=outcome.model,
        detected_language=language.value,
    )
