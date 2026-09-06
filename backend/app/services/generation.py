"""generate_reply — the single seam of the paid pipeline.

Order (SPEC §6.7): guardrails -> FAQ -> semantic cache -> circuit breaker
-> atomic budget reserve -> RAG -> context -> model -> cost/settle ->
output validation -> cache store.

The function persists NOTHING into messages/wa_outbox — that belongs to
the caller (worker pipeline or sandbox endpoint). What it DOES persist,
in its own short transactions, is money truth: budget reservations,
settlements and ai_calls rows — real spending may never go unaccounted,
including sandbox runs (chat_id NULL there).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from app.ai import guardrails
from app.ai.chat_provider import ChatError, ChatRequest, ChatUsage
from app.ai.context_builder import ContextInput, HistoryTurn, build_context
from app.models import AiCall, SystemEvent
from app.models.enums import CallType, KbLanguage
from app.services import app_settings, budget, semantic_cache
from app.services.agent import get_active_instruction
from app.services.app_settings import get_setting
from app.services.circuit_breaker import CircuitBreaker
from app.services.kb_faq import match_faq
from app.services.kb_search import search_chunks
from app.services.pricing import (
    PricingError,
    Usage,
    compute_cost_usd,
    estimate_max_cost_usd,
    load_price_sheet,
    load_usd_kzt_rate,
    usd_to_kzt,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.ai.chat_provider import ChatProvider
    from app.ai.embeddings import EmbeddingProvider
    from app.config import Settings
    from app.models import AgentInstruction

logger = structlog.get_logger(__name__)

# Built-in trilingual templates; the owner's instruction overrides them
# via fallback_templates. White-label: no company names here.
DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "handoff": {
        "ru": "Передал ваш вопрос менеджеру — с вами свяжутся в ближайшее время.",
        "kk": "Сұрағыңызды менеджерге жеткіздім — жақын арада сізбен хабарласады.",
        "en": "I've passed your question to a manager — they will contact you shortly.",
    },
    "offtopic": {
        "ru": "Могу отвечать только на вопросы об услугах нашего центра.",
        "kk": "Мен тек орталығымыздың қызметтері туралы сұрақтарға жауап бере аламын.",
        "en": "I can only answer questions about our center's services.",
    },
    "too_long": {
        "ru": "Сообщение слишком длинное. Пожалуйста, сформулируйте вопрос короче.",
        "kk": "Хабарлама тым ұзын. Сұрақты қысқаша жазыңызшы.",
        "en": "The message is too long. Please ask a shorter question.",
    },
}
# Injection attempts and blocked-by-limit both answer with safe templates.
_GUARDRAIL_TEMPLATE_KEYS = {"injection": "offtopic", "offtopic": "offtopic", "too_long": "too_long"}


def pick_template(instruction: AgentInstruction | None, key: str, language: KbLanguage) -> str:
    """Owner template for the client's language, else built-in for that
    language, else the ru built-in (system texts, not FAQ answers — the
    owner's no-Russian-substitute rule governs FAQ content only)."""
    lang = language.value
    if instruction is not None:
        owner = instruction.fallback_templates.get(key)
        if isinstance(owner, dict):
            candidate = owner.get(lang)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if isinstance(owner, str) and owner.strip():
            return owner.strip()
    defaults = DEFAULT_TEMPLATES.get(key, DEFAULT_TEMPLATES["handoff"])
    return defaults.get(lang, defaults["ru"])


@dataclass
class GenerationDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    redis: Redis
    chat_provider: ChatProvider | None
    embedding_provider: EmbeddingProvider | None


@dataclass(frozen=True)
class GenerationInput:
    correlation_id: str
    question: str
    language: KbLanguage
    chat_id: int | None = None
    phone: str | None = None
    history: list[HistoryTurn] = field(default_factory=list)
    memory: dict[str, Any] | None = None
    summary: str | None = None
    merged_count: int = 1
    # Retry attempt number. Baked into the reservation sub-correlation:
    # a NEW attempt takes a NEW reservation (the previous one is already
    # released, and a released reservation is terminal by design), while
    # a crash-retry WITHIN one attempt stays idempotent.
    attempt: int = 0


@dataclass
class GenerationOutcome:
    kind: str  # guardrail|faq|cache|breaker_open|not_configured|blocked_limit|no_data|model|error
    text: str | None = None
    error_code: str | None = None
    retryable: bool = False
    handoff: bool = False  # the caller sets needs_attention on the chat
    cost_usd: Decimal = Decimal(0)
    cost_kzt: Decimal = Decimal(0)
    usage: ChatUsage | None = None
    latency_ms: int | None = None
    kb_chunk_ids: list[int] = field(default_factory=list)
    cache_hit: bool = False
    instruction_version: int | None = None
    model: str | None = None
    ai_call_id: int | None = None

    @property
    def paid(self) -> bool:
        return self.kind == "model"


async def _record_ai_call(
    deps: GenerationDeps,
    *,
    inp: GenerationInput,
    call_type: CallType,
    model: str,
    usage: ChatUsage | None = None,
    cost_usd: Decimal = Decimal(0),
    cost_kzt: Decimal = Decimal(0),
    latency_ms: int | None = None,
    kb_chunk_ids: list[int] | None = None,
    instruction_version: int | None = None,
    reasoning_effort: str | None = None,
    status: str = "ok",
    error_code: str | None = None,
) -> int:
    """Own committed transaction: the accounting row must survive
    whatever happens to the reply afterwards."""
    used = usage or ChatUsage()
    async with deps.sessionmaker() as db:
        call = AiCall(
            correlation_id=inp.correlation_id,
            chat_id=inp.chat_id,
            call_type=call_type,
            model=model,
            input_tokens=used.input_tokens,
            cached_input_tokens=used.cached_input_tokens,
            output_tokens=used.output_tokens,
            reasoning_tokens=used.reasoning_tokens,
            cost_usd=cost_usd,
            cost_kzt=cost_kzt,
            latency_ms=latency_ms,
            rag_chunk_ids=kb_chunk_ids,
            instruction_version=instruction_version,
            reasoning_effort=reasoning_effort,
            status=status,
            error_code=error_code,
        )
        db.add(call)
        await db.flush()
        call_id = call.id
        await db.commit()
    return call_id


async def _system_event(
    deps: GenerationDeps, *, level: str, event: str, detail: dict[str, Any]
) -> None:
    async with deps.sessionmaker() as db:
        db.add(SystemEvent(level=level, component="ai_pipeline", event=event, detail=detail))
        await db.commit()


async def generate_reply(deps: GenerationDeps, inp: GenerationInput) -> GenerationOutcome:  # noqa: PLR0911, PLR0912, PLR0915 - the pipeline order IS the function
    sub_correlation = f"{inp.correlation_id}:main:a{inp.attempt}"

    # --- free gates: instruction, guardrails, FAQ, cache ----------------
    async with deps.sessionmaker() as db:
        instruction = await get_active_instruction(db)
        if instruction is None:
            return GenerationOutcome(kind="not_configured", error_code="no_active_instruction")

        verdict = guardrails.check_input(inp.question)
        if not verdict.ok:
            template_key = _GUARDRAIL_TEMPLATE_KEYS.get(verdict.reason or "", "offtopic")
            return GenerationOutcome(
                kind="guardrail",
                text=pick_template(instruction, template_key, inp.language),
                error_code=verdict.reason,
                instruction_version=instruction.version,
            )

        faq = await match_faq(db, inp.question, inp.language, chat_id=inp.chat_id)
        cache_enabled = bool(await get_setting(db, app_settings.SEMANTIC_CACHE_ENABLED))
        kb_version = await semantic_cache.get_kb_version(db)
        cached_answer: str | None = None
        if cache_enabled and faq is None and inp.merged_count == 1:
            cached_answer = await semantic_cache.lookup(
                db, inp.question, inp.language, None, kb_version
            )
        max_input_tokens = int(await get_setting(db, app_settings.MAX_INPUT_TOKENS))
        max_output_tokens = int(await get_setting(db, app_settings.MAX_OUTPUT_TOKENS))
        breaker_failures = int(await get_setting(db, app_settings.CIRCUIT_BREAKER_FAILURES))
        breaker_cooldown = int(await get_setting(db, app_settings.CIRCUIT_BREAKER_COOLDOWN_MINUTES))
        cache_ttl_hours = int(await get_setting(db, app_settings.SEMANTIC_CACHE_TTL_HOURS))
        allowed_domains = list(await get_setting(db, app_settings.ALLOWED_LINK_DOMAINS))
        await db.commit()  # persists FAQ miss records and cache hit counts

    if faq is not None:
        return GenerationOutcome(
            kind="faq", text=faq.answer, instruction_version=instruction.version
        )
    if cached_answer is not None:
        return GenerationOutcome(
            kind="cache",
            text=cached_answer,
            cache_hit=True,
            instruction_version=instruction.version,
        )

    # --- breaker: paid path only; free answers above never see it -------
    breaker = CircuitBreaker(
        deps.redis, failures_threshold=breaker_failures, cooldown_minutes=breaker_cooldown
    )
    if await breaker.is_open():
        return GenerationOutcome(kind="breaker_open", error_code="circuit_breaker_open")

    if deps.chat_provider is None or deps.embedding_provider is None:
        return GenerationOutcome(kind="not_configured", error_code="openai_not_configured")

    model = instruction.model or deps.settings.openai_chat_model
    reasoning_effort = instruction.reasoning_effort or deps.settings.openai_reasoning_effort
    output_cap = instruction.max_output_tokens or max_output_tokens

    # --- reserve (fail closed on missing prices/rate) --------------------
    try:
        async with deps.sessionmaker() as db:
            prices = await load_price_sheet(db, model)
            rate = await load_usd_kzt_rate(db)
    except PricingError as exc:
        return GenerationOutcome(kind="not_configured", error_code=f"pricing:{exc}")
    estimate = estimate_max_cost_usd(prices, max_input_tokens, output_cap)
    reserved = await budget.reserve(
        deps.sessionmaker,
        sub_correlation=sub_correlation,
        estimate_usd=estimate,
        chat_id=inp.chat_id,
        phone=inp.phone,
    )
    if not reserved.allowed:
        return GenerationOutcome(
            kind="blocked_limit",
            text=pick_template(instruction, "handoff", inp.language),
            error_code=f"blocked_by_limit:{reserved.blocked_scope}",
            handoff=True,
            instruction_version=instruction.version,
        )

    try:
        # --- RAG (accounted embedding call) ------------------------------
        async with deps.sessionmaker() as db:
            search = await search_chunks(db, deps.embedding_provider, deps.settings, inp.question)
            embed_cost = Decimal(0)
            try:
                embed_prices = await load_price_sheet(db, search.embed_model)
                embed_cost = compute_cost_usd(embed_prices, Usage(input_tokens=search.embed_tokens))
            except PricingError:
                # Embedding accounting degrades to zero-cost rows, but the
                # call itself is still visible; the model spend gate is the
                # reservation above, which already failed closed.
                logger.warning("embed_price_missing", model=search.embed_model)
        await _record_ai_call(
            deps,
            inp=inp,
            call_type=CallType.EMBEDDING,
            model=search.embed_model,
            usage=ChatUsage(input_tokens=search.embed_tokens),
            cost_usd=embed_cost,
            cost_kzt=usd_to_kzt(embed_cost, rate),
        )

        if not search.hits:
            # SPEC §6.9: below threshold -> template/handoff, NO model call.
            await budget.release(deps.sessionmaker, sub_correlation=sub_correlation)
            rules = instruction.escalation_rules or {}
            template_key = "offtopic" if rules.get("on_no_data") == "offtopic" else "handoff"
            return GenerationOutcome(
                kind="no_data",
                text=pick_template(instruction, template_key, inp.language),
                handoff=template_key == "handoff",
                instruction_version=instruction.version,
            )

        # --- context + model ---------------------------------------------
        built = build_context(
            ContextInput(
                system_prompt=instruction.system_prompt,
                style=instruction.style,
                memory=inp.memory,
                summary=inp.summary,
                chunks=search.hits,
                history=inp.history,
                question=inp.question,
                max_input_tokens=max_input_tokens,
            )
        )
        request = ChatRequest(
            system=built.system,
            messages=built.messages,
            model=model,
            max_output_tokens=output_cap,
            reasoning_effort=reasoning_effort,
        )
        try:
            result = await deps.chat_provider.complete(request)
        except ChatError as exc:
            await budget.release(deps.sessionmaker, sub_correlation=sub_correlation)
            await _record_ai_call(
                deps,
                inp=inp,
                call_type=CallType.CHAT,
                model=model,
                kb_chunk_ids=built.kb_chunk_ids,
                instruction_version=instruction.version,
                reasoning_effort=reasoning_effort,
                status="error",
                error_code=str(exc)[:200],
            )
            if await breaker.record_failure():
                await _system_event(
                    deps,
                    level="error",
                    event="circuit_breaker_opened",
                    detail={"correlation_id": inp.correlation_id, "error": str(exc)[:200]},
                )
            return GenerationOutcome(
                kind="error",
                error_code=str(exc)[:200],
                retryable=exc.retryable,
                instruction_version=instruction.version,
            )

        await breaker.record_success()
        cost_usd = compute_cost_usd(
            prices,
            Usage(
                input_tokens=result.usage.input_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                output_tokens=result.usage.output_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
            ),
        )
        cost_kzt = usd_to_kzt(cost_usd, rate)
        ai_call_id = await _record_ai_call(
            deps,
            inp=inp,
            call_type=CallType.CHAT,
            model=result.model,
            usage=result.usage,
            cost_usd=cost_usd,
            cost_kzt=cost_kzt,
            latency_ms=result.latency_ms,
            kb_chunk_ids=built.kb_chunk_ids,
            instruction_version=instruction.version,
            reasoning_effort=reasoning_effort,
        )
        await budget.settle(deps.sessionmaker, sub_correlation=sub_correlation, actual_usd=cost_usd)

        # --- output validation --------------------------------------------
        text = result.text.strip()
        handoff = False
        out_verdict = guardrails.check_output(text, allowed_link_domains=allowed_domains)
        if not out_verdict.ok:
            logger.warning(
                "output_rejected", reason=out_verdict.reason, correlation_id=inp.correlation_id
            )
            await _system_event(
                deps,
                level="warning",
                event="output_rejected",
                detail={"reason": out_verdict.reason, "correlation_id": inp.correlation_id},
            )
            text = pick_template(instruction, "handoff", inp.language)
            handoff = True

        # --- cache store (only clean, impersonal, single-question hits) ---
        cacheable = (
            cache_enabled
            and not handoff
            and inp.merged_count == 1
            and not inp.memory
            and bool(search.hits)
        )
        if cacheable:
            async with deps.sessionmaker() as db:
                await semantic_cache.store(
                    db, inp.question, inp.language, None, kb_version, text, cache_ttl_hours
                )
                await db.commit()

        return GenerationOutcome(
            kind="model",
            text=text,
            handoff=handoff,
            cost_usd=cost_usd,
            cost_kzt=cost_kzt,
            usage=result.usage,
            latency_ms=result.latency_ms,
            kb_chunk_ids=built.kb_chunk_ids,
            instruction_version=instruction.version,
            model=result.model,
            ai_call_id=ai_call_id,
        )
    except Exception:
        # Unexpected failure between reserve and settle: money comes back.
        await budget.release(deps.sessionmaker, sub_correlation=sub_correlation)
        raise


def new_sandbox_correlation() -> str:
    return f"sandbox:{uuid.uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(UTC)
