"""AI assistance for the knowledge base: translations and trigger phrases.

Owner decision (2026-09-11): writing Kazakh/English answers and inventing
every way a client might phrase a question by hand is not work a human
should do — the model does it and the owner reviews.

The language policy is NOT relaxed by this module:
- a generated translation is stored with status `machine`, which
  `approved_answer()` never delivers to a client. It is a draft for the
  owner's per-entry approval, nothing more;
- Russian stays the source of truth and is never machine-written.

Money: every call here is a real paid call, so it follows the same
discipline as the rest of the project — atomic budget reservation before
the request, an `ai_calls` row with the factual usage, settle after.
Nothing runs in the background: the owner clicks, waits a second, sees
the result.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from app.ai.chat_provider import ChatError, ChatRequest, ChatUsage
from app.ai.tokens import count_tokens
from app.models import AiCall
from app.models.enums import CallType, KbLanguage
from app.services import budget
from app.services.pricing import (
    PriceSheet,
    PricingError,
    Usage,
    compute_cost_usd,
    estimate_max_cost_usd,
    load_price_sheet,
    load_usd_kzt_rate,
    usd_to_kzt,
)

if TYPE_CHECKING:
    from app.services.generation import GenerationDeps

logger = structlog.get_logger(__name__)

LANGUAGE_NAMES: dict[KbLanguage, str] = {
    KbLanguage.RU: "русском",
    KbLanguage.KK: "казахском",
    KbLanguage.EN: "английском",
}

# A translation can legitimately be a little longer than its source
# (Kazakh runs longer than Russian), never dramatically so.
_TRANSLATION_OVERHEAD_TOKENS = 120
_TRANSLATION_MAX_TOKENS = 700
_PATTERNS_MAX_TOKENS = 400
_PATTERNS_WANTED = 8
_PATTERN_MAX_CHARS = 120
# Strips "1.", "-", "•", quotes — models decorate lists no matter what
# the prompt says.
_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•—])\s*")
_QUOTES = "\"'«»„“”`"


class KbAiError(Exception):
    """Generation failed or was refused. `retryable` tells the API
    whether a 503 (try later) or a 422 (do not retry) is honest."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class AiTextResult:
    text: str
    cost_usd: Decimal
    cost_kzt: Decimal


@dataclass(frozen=True)
class AiPatternsResult:
    patterns: list[str]
    cost_usd: Decimal
    cost_kzt: Decimal


async def translate_answer(
    deps: GenerationDeps,
    *,
    faq_id: int,
    source_ru: str,
    target: KbLanguage,
) -> AiTextResult:
    """Translate an approved Russian answer. The result is a DRAFT: the
    caller stores it with status `machine`, never `approved`."""
    if target is KbLanguage.RU:
        msg = "Русский ответ пишет владелец, он источник истины"
        raise KbAiError(msg)
    system = (
        "Ты профессиональный переводчик службы поддержки. Переведи ответ оператора "
        f"на {LANGUAGE_NAMES[target]} язык. Требования: сохрани смысл дословно; "
        "цифры, цены, адреса, телефоны, ссылки и названия оставь без изменений; "
        "сохрани деловой вежливый тон и структуру строк; ничего не добавляй от себя "
        "и ничего не комментируй. Верни ТОЛЬКО перевод."
    )
    max_tokens = min(
        _TRANSLATION_MAX_TOKENS, count_tokens(source_ru) * 2 + _TRANSLATION_OVERHEAD_TOKENS
    )
    result = await _paid_call(
        deps,
        correlation_id=f"faq:{faq_id}:translate:{target.value}",
        system=system,
        user=source_ru,
        max_output_tokens=max_tokens,
    )
    text = result.text.strip().strip(_QUOTES).strip()
    if not text:
        msg = "Модель вернула пустой перевод"
        raise KbAiError(msg, retryable=True)
    return AiTextResult(text=text, cost_usd=result.cost_usd, cost_kzt=result.cost_kzt)


async def suggest_patterns(
    deps: GenerationDeps,
    *,
    faq_id: int,
    topic: str,
    answer_ru: str,
    target: KbLanguage,
    existing: list[str],
) -> AiPatternsResult:
    """Propose ways a customer might ask this question in one language.

    Matching is exact (normalized text or word set), so coverage comes
    from having the phrasings people actually type — exactly the kind of
    listing a human is bad at and a model is good at.
    """
    system = (
        "Ты помогаешь настроить автоответчик в WhatsApp. По теме и готовому ответу "
        f"придумай {_PATTERNS_WANTED} коротких вариантов того, КАК реальный клиент "
        f"задаёт этот вопрос в мессенджере на {LANGUAGE_NAMES[target]} языке. "
        "Правила: только сам вопрос или фраза клиента, без кавычек и нумерации; "
        "по одному варианту в строке; разговорно и коротко, как пишут в чате "
        "(в том числе без знаков вопроса и с обиходными словами); "
        "не повторяй уже имеющиеся варианты; не пиши ничего, кроме списка строк."
    )
    user_parts = [f"Тема: {topic}", f"Ответ, который получит клиент:\n{answer_ru}"]
    if existing:
        user_parts.append("Уже есть такие варианты (не повторяй их):\n" + "\n".join(existing[:40]))
    result = await _paid_call(
        deps,
        correlation_id=f"faq:{faq_id}:patterns:{target.value}",
        system=system,
        user="\n\n".join(user_parts),
        max_output_tokens=_PATTERNS_MAX_TOKENS,
    )
    patterns = _parse_patterns(result.text, existing)
    if not patterns:
        msg = "Модель не предложила ни одной новой формулировки"
        raise KbAiError(msg, retryable=True)
    return AiPatternsResult(patterns=patterns, cost_usd=result.cost_usd, cost_kzt=result.cost_kzt)


def _parse_patterns(raw: str, existing: list[str]) -> list[str]:
    """Model output is untrusted text: take lines, drop decoration, drop
    anything that is not a plausible customer phrase."""
    seen = {item.strip().casefold() for item in existing}
    result: list[str] = []
    for line in raw.splitlines():
        candidate = _BULLET_RE.sub("", line).strip().strip(_QUOTES).strip()
        if not candidate or len(candidate) > _PATTERN_MAX_CHARS:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= _PATTERNS_WANTED:
            break
    return result


async def _book_failed_call(
    deps: GenerationDeps,
    *,
    usage: ChatUsage,
    prices: PriceSheet,
    model: str,
    correlation_id: str,
    sub_correlation: str,
) -> None:
    """Account for a paid call that returned nothing usable."""
    cost = compute_cost_usd(
        prices,
        Usage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        ),
    )
    async with deps.sessionmaker() as db:
        try:
            rate: Decimal | None = await load_usd_kzt_rate(db)
        except PricingError:
            rate = None
        db.add(
            AiCall(
                correlation_id=correlation_id,
                chat_id=None,
                call_type=CallType.CHAT,
                model=model,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cost_usd=cost,
                cost_kzt=usd_to_kzt(cost, rate) if rate is not None else Decimal(0),
                status="error",
                error_code="empty_response",
            )
        )
        await db.commit()
    await budget.settle(deps.sessionmaker, sub_correlation=sub_correlation, actual_usd=cost)


@dataclass(frozen=True)
class _CallResult:
    text: str
    cost_usd: Decimal
    cost_kzt: Decimal


async def _paid_call(
    deps: GenerationDeps,
    *,
    correlation_id: str,
    system: str,
    user: str,
    max_output_tokens: int,
) -> _CallResult:
    """One paid chat call with the project's money discipline: reserve,
    call, record, settle. Fails closed — no prices, no call."""
    if deps.chat_provider is None:
        msg = "Не настроен ключ OpenAI — автогенерация недоступна"
        raise KbAiError(msg)
    model = deps.settings.openai_chat_model
    try:
        async with deps.sessionmaker() as db:
            prices = await load_price_sheet(db, model)
    except PricingError as exc:
        msg = "Не заданы цены модели — заполните их в разделе «Лимиты»"
        raise KbAiError(msg) from exc

    estimate = estimate_max_cost_usd(
        prices, count_tokens(system) + count_tokens(user) + 50, max_output_tokens
    )
    # A fresh id per click. With a fixed suffix the UNIQUE
    # (correlation_id, ledger_id) makes the SECOND reservation a no-op
    # (ON CONFLICT DO NOTHING) that still reports success — the call
    # would be paid for outside the budget and never reach spent_usd.
    sub_correlation = f"{correlation_id}:{uuid4().hex[:12]}"
    try:
        reserved = await budget.reserve(
            deps.sessionmaker,
            sub_correlation=sub_correlation,
            chat_id=None,
            phone=None,
            estimate_usd=estimate,
        )
    except PricingError as exc:
        # Reserving needs the USD/KZT rate too; without it the owner
        # should see what to fix, not a 500.
        msg = "Не задан курс USD/KZT — укажите его в разделе «Лимиты»"
        raise KbAiError(msg) from exc
    if not reserved.allowed:
        msg = "Достигнут лимит расходов — автогенерация остановлена"
        raise KbAiError(msg)

    started = time.monotonic()
    try:
        completion = await deps.chat_provider.complete(
            ChatRequest(
                system=system,
                messages=[{"role": "user", "content": user}],
                model=model,
                max_output_tokens=max_output_tokens,
                reasoning_effort=deps.settings.openai_reasoning_effort,
            )
        )
    except ChatError as exc:
        # A billed-but-empty answer is still spend: record and settle it,
        # otherwise the limits never see money that actually left.
        if exc.usage is None:
            await budget.release(deps.sessionmaker, sub_correlation=sub_correlation)
        else:
            await _book_failed_call(
                deps,
                usage=exc.usage,
                prices=prices,
                model=model,
                correlation_id=sub_correlation,
                sub_correlation=sub_correlation,
            )
        logger.warning("kb_ai_call_failed", correlation_id=correlation_id, error=str(exc))
        msg = "Модель не ответила, попробуйте ещё раз"
        raise KbAiError(msg, retryable=exc.retryable) from exc

    usage = completion.usage
    cost_usd = compute_cost_usd(
        prices,
        Usage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        ),
    )
    async with deps.sessionmaker() as db:
        try:
            rate: Decimal | None = await load_usd_kzt_rate(db)
        except PricingError:
            rate = None
        cost_kzt = usd_to_kzt(cost_usd, rate) if rate is not None else Decimal(0)
        db.add(
            AiCall(
                # The exact call, so a repeat is a separate row and the
                # accounting matches the reservation one-to-one.
                correlation_id=sub_correlation,
                chat_id=None,  # admin-side work, not a customer dialog
                call_type=CallType.CHAT,
                model=completion.model,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cost_usd=cost_usd,
                cost_kzt=cost_kzt,
                latency_ms=int((time.monotonic() - started) * 1000),
                status="ok",
            )
        )
        await db.commit()
    await budget.settle(deps.sessionmaker, sub_correlation=sub_correlation, actual_usd=cost_usd)
    return _CallResult(text=completion.text, cost_usd=cost_usd, cost_kzt=cost_kzt)
