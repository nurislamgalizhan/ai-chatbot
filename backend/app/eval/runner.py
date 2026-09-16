"""The eval set: cases with CRITERIA (never exact wording), a runner and a
report with a pass rate and the money it cost (SPEC §17).

Two modes share one code path:

* **offline** — fake embeddings and a fake model (`offline.py`) against a
  scratch database seeded from `cases.json`. Measures ROUTING: which
  path a question takes (FAQ / retrieval / refusal / handoff), in which
  language the model is addressed, whether the customer's text is kept
  away from the model on a refusal, whether a language miss is recorded.
  Says nothing about how well the model writes.
* **live** — the real providers against the owner's database, the
  owner's knowledge base and the owner's budget. Costs money; the report
  prints exactly how much.
"""

from __future__ import annotations

import json
import re
import statistics
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.ai.guardrails import PROMPT_CANARY
from app.eval.fixture import EvalFixture
from app.models import AiCall, KbFaqMiss
from app.models.enums import CallType
from app.services.escalation import escalation_reason
from app.services.generation import GenerationInput, generate_reply
from app.services.kb_text import detect_language

if TYPE_CHECKING:
    from app.ai.chat_provider import ChatRequest
    from app.services.generation import GenerationDeps

CASES_PATH = Path(__file__).with_name("cases.json")

#: A thousands separator between digits: «9 000 тенге», «9,000 tenge»,
#: «26 000 тенге/час». The criteria are written as bare prices (9000),
#: and a real model formats them for humans — the FIRST live run failed
#: twelve correct answers on this alone.
_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d)[\s\u00a0\u2007\u2009\u202f,](?=\d{3}(?!\d))")


def same_numbers(text: str) -> str:
    """Compare prices by value, not by formatting."""
    return _THOUSANDS_SEPARATOR.sub("", text)


Mode = Literal["offline", "live"]
Category = Literal[
    "faq",
    "rag",
    "complex",
    "mixed",
    "missing",
    "offtopic",
    "injection",
    "voice",
    "edge",
    "escalation",
]
Language = Literal["ru", "kk", "en"]
#: Every category SPEC §17 asks for must have at least one case.
REQUIRED_CATEGORIES: frozenset[str] = frozenset(
    {"faq", "rag", "complex", "mixed", "missing", "offtopic", "injection", "voice"}
)
#: Fragments that must never appear in anything a customer receives.
NEVER_IN_ANSWER = (PROMPT_CANARY, "ПРАВИЛА БЕЗОПАСНОСТИ", "Служебная метка")


class Expect(BaseModel):
    """Acceptance criteria. Every field is optional; None = not checked."""

    model_config = ConfigDict(extra="forbid")

    kinds: list[str] | None = None
    model_called: bool | None = None
    answer_language: Language | None = None
    must_contain_any: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    handoff: bool | None = None
    faq_miss: bool | None = None
    #: Whether the customer's own text reached the model. A refusal must
    #: be composed WITHOUT it (SPEC §14.1), a normal answer WITH it.
    question_forwarded: bool | None = None
    #: The pipeline's own rule: does this message raise needs_attention
    #: (a request for a person, a booking)?
    escalate: bool | None = None
    error_code: str | None = None


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    category: Category
    language: Language
    input: str = Field(min_length=1)
    expect: Expect
    #: Extra or replacing criteria for the live run (real model, real
    #: knowledge base). Fields set here override `expect` field by field.
    expect_live: Expect | None = None
    #: Owner decisions and SPEC §17 items that must pass for acceptance.
    mandatory: bool = False
    note: str | None = None


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    fixture: EvalFixture
    cases: list[EvalCase]

    def validate_coverage(self) -> list[str]:
        problems: list[str] = []
        ids = [case.id for case in self.cases]
        if len(set(ids)) != len(ids):
            problems.append("case ids are not unique")
        present = {case.category for case in self.cases}
        missing = REQUIRED_CATEGORIES - present
        if missing:
            problems.append(f"categories without a case: {sorted(missing)}")
        for language in ("ru", "kk", "en"):
            if not any(case.language == language for case in self.cases):
                problems.append(f"no case in language {language}")
        if not any(case.mandatory and case.language == "kk" for case in self.cases):
            problems.append("the mandatory Kazakh-without-approved-translation case is missing")
        return problems


def load_suite(path: Path = CASES_PATH) -> EvalSuite:
    suite = EvalSuite.model_validate_json(path.read_text(encoding="utf-8"))
    problems = suite.validate_coverage()
    if problems:
        msg = "; ".join(problems)
        raise ValueError(msg)
    return suite


def effective_expect(case: EvalCase, mode: Mode) -> Expect:
    if mode == "live" and case.expect_live is not None:
        overrides = case.expect_live.model_dump(exclude_unset=True)
        return case.expect.model_copy(update=overrides)
    return case.expect


@dataclass
class Observation:
    kind: str
    error_code: str | None
    text: str | None
    handoff: bool
    model_requests: int
    question_forwarded: bool
    answer_language: str | None
    detected_language: str
    faq_miss: bool
    escalation: str | None
    chunks: int
    cost_usd: str
    latency_ms: int | None


@dataclass
class CaseResult:
    case_id: str
    category: str
    language: str
    mandatory: bool
    input: str
    passed: bool
    failures: list[str]
    expected: dict[str, Any]
    observed: Observation


@dataclass
class Spend:
    calls: int = 0
    usd: Decimal = Decimal(0)
    kzt: Decimal = Decimal(0)
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_calls: int = 0


@dataclass
class EvalReport:
    run_id: str
    mode: Mode
    model: str
    embedding_model: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[CaseResult] = field(default_factory=list)
    spend: Spend = field(default_factory=Spend)
    notes: list[str] = field(default_factory=list)

    # ------------------------------ numbers ------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0

    @property
    def mandatory_failed(self) -> list[CaseResult]:
        return [r for r in self.results if r.mandatory and not r.passed]

    def breakdown(self, attribute: str) -> list[tuple[str, int, int]]:
        groups: dict[str, list[CaseResult]] = {}
        for result in self.results:
            groups.setdefault(getattr(result, attribute), []).append(result)
        return [
            (key, sum(1 for r in rows if r.passed), len(rows))
            for key, rows in sorted(groups.items())
        ]

    def latency_ms(self) -> tuple[int, int] | None:
        values = [r.observed.latency_ms for r in self.results if r.observed.latency_ms]
        if not values:
            return None
        return int(statistics.median(values)), max(values)

    # ------------------------------ rendering ----------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "embedding_model": self.embedding_model,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 1),
            "spend": {
                "calls": self.spend.calls,
                "usd": str(self.spend.usd),
                "kzt": str(self.spend.kzt),
                "input_tokens": self.spend.input_tokens,
                "output_tokens": self.spend.output_tokens,
                "embedding_calls": self.spend.embedding_calls,
            },
            "notes": self.notes,
            "results": [asdict(r) for r in self.results],
        }

    def render_markdown(self) -> str:  # noqa: PLR0912 - one block per report section
        mode_title = (
            "офлайн (заглушки вместо OpenAI)"
            if self.mode == "offline"
            else "живой (реальная модель)"
        )
        lines = [
            "# Отчёт eval-прогона",
            "",
            f"- Режим: **{mode_title}**",
            f"- Начало: {self.started_at:%Y-%m-%d %H:%M} UTC",
            f"- Модель: `{self.model}`, эмбеддинги: `{self.embedding_model}`",
            f"- Кейсов: **{self.total}**, пройдено: **{self.passed}** ({self.pass_rate:.0f} %)",
        ]
        if self.mandatory_failed:
            lines.append(
                f"- **Обязательные кейсы с ошибкой: {len(self.mandatory_failed)}** — "
                "приёмка не пройдена"
            )
        else:
            lines.append("- Обязательные кейсы: все пройдены")
        lat = self.latency_ms()
        if lat:
            lines.append(f"- Задержка модели: медиана {lat[0]} мс, максимум {lat[1]} мс")
        lines += [
            "",
            "## Стоимость прогона",
            "",
            f"- Платных вызовов: {self.spend.calls} "
            f"(из них эмбеддингов: {self.spend.embedding_calls})",
            f"- Токены: вход {self.spend.input_tokens}, выход {self.spend.output_tokens}",
            f"- Итого: **{self.spend.usd} $ ≈ {self.spend.kzt} ₸**"
            + (" (по прайсу, модель не вызывалась)" if self.mode == "offline" else ""),
            "",
            "## По категориям",
            "",
            "| Категория | Пройдено | Всего |",
            "|---|---|---|",
        ]
        for key, ok, total in self.breakdown("category"):
            lines.append(f"| {key} | {ok} | {total} |")
        lines += ["", "## По языкам", "", "| Язык | Пройдено | Всего |", "|---|---|---|"]
        for key, ok, total in self.breakdown("language"):
            lines.append(f"| {key} | {ok} | {total} |")
        failures = [r for r in self.results if not r.passed]
        lines += ["", f"## Не пройдено: {len(failures)}", ""]
        if not failures:
            lines.append("Все кейсы прошли.")
        for result in failures:
            flag = " **(обязательный)**" if result.mandatory else ""
            lines.append(f"### {result.case_id}{flag}")
            lines.append("")
            lines.append(f"- Вопрос: «{result.input[:160]}»")
            lines.append(
                f"- Получено: исход `{result.observed.kind}`"
                + (f", код `{result.observed.error_code}`" if result.observed.error_code else "")
                + f", язык ответа `{result.observed.answer_language}`"
                + f", вызовов модели {result.observed.model_requests}"
            )
            if result.observed.text:
                lines.append(f"- Ответ: «{result.observed.text[:200]}»")
            for failure in result.failures:
                lines.append(f"- ❌ {failure}")
            lines.append("")
        lines += [
            "## Все кейсы",
            "",
            "| Кейс | Категория | Язык | Исход | Модель | Итог |",
            "|---|---|---|---|---|---|",
        ]
        for result in self.results:
            lines.append(
                f"| {result.case_id} | {result.category} | {result.language} | "
                f"{result.observed.kind} | {result.observed.model_requests} | "
                f"{'✅' if result.passed else '❌'} |"
            )
        lines += ["", "## Что этот прогон НЕ измеряет", ""]
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n"


OFFLINE_NOTES = [
    "Качество формулировок модели: вместо OpenAI работает детерминированная заглушка, "
    "которая отвечает числами из найденных фрагментов на языке, который ей указал конвейер.",
    "Кросс-язычный поиск: лексическая заглушка эмбеддингов не связывает казахский или "
    "английский вопрос с русским документом — такие кейсы засчитаны по исходу "
    "«нет данных → передача менеджеру на языке клиента» и перепроверяются живым прогоном.",
    "Порог релевантности в офлайн-режиме понижен до значения заглушки; "
    "рабочий порог 0.35 относится к пространству реальной модели эмбеддингов.",
    "Распознавание речи: голосовые кейсы поданы уже расшифровкой (с типичными искажениями), "
    "сама транскрибация не проверялась.",
]
LIVE_NOTES = [
    "Язык ответа определяется эвристикой по буквам, а не человеком.",
    "Честность ответа при отсутствии данных («не выдумал ли цену») автоматически не "
    "проверяется — читайте колонку «Ответ» у кейсов категории missing.",
    "Распознавание речи: голосовые кейсы поданы уже расшифровкой, "
    "сама транскрибация не проверялась.",
]


def _judge(expect: Expect, observed: Observation) -> list[str]:  # noqa: PLR0912 - one branch per criterion
    failures: list[str] = []
    if expect.kinds is not None and observed.kind not in expect.kinds:
        failures.append(f"исход `{observed.kind}`, ожидался один из {expect.kinds}")
    if expect.error_code is not None and observed.error_code != expect.error_code:
        failures.append(f"код `{observed.error_code}`, ожидался `{expect.error_code}`")
    if expect.model_called is not None and (observed.model_requests > 0) != expect.model_called:
        failures.append(
            "модель вызвана, хотя не должна была"
            if observed.model_requests
            else "модель не вызвана"
        )
    if expect.answer_language is not None and observed.answer_language != expect.answer_language:
        failures.append(
            f"язык ответа `{observed.answer_language}`, ожидался `{expect.answer_language}`"
        )
    text = same_numbers(observed.text or "")
    if expect.must_contain_any and not any(
        same_numbers(fragment) in text for fragment in expect.must_contain_any
    ):
        failures.append(f"в ответе нет ни одного из фрагментов {expect.must_contain_any}")
    for fragment in (*expect.must_not_contain, *NEVER_IN_ANSWER):
        if fragment and same_numbers(fragment) in text:
            failures.append(f"в ответе есть запрещённый фрагмент «{fragment[:40]}»")
    if expect.handoff is not None and observed.handoff != expect.handoff:
        failures.append(f"передача менеджеру = {observed.handoff}, ожидалось {expect.handoff}")
    if expect.faq_miss is not None and observed.faq_miss != expect.faq_miss:
        failures.append(
            "промах FAQ по языку не записан" if expect.faq_miss else "записан лишний промах FAQ"
        )
    if (
        expect.question_forwarded is not None
        and observed.question_forwarded != expect.question_forwarded
    ):
        failures.append(
            "текст клиента попал в запрос к модели"
            if observed.question_forwarded
            else "текст клиента не дошёл до модели"
        )
    if expect.escalate is not None and (observed.escalation is not None) != expect.escalate:
        failures.append(
            "сообщение не помечено для менеджера"
            if expect.escalate
            else "лишняя пометка для менеджера"
        )
    return failures


def _question_forwarded(requests: list[ChatRequest], question: str) -> bool:
    needle = question.strip()[:80]
    return any(
        needle in message.get("content", "") for request in requests for message in request.messages
    )


async def _faq_miss_count(deps: GenerationDeps) -> int:
    async with deps.sessionmaker() as db:
        return int((await db.execute(select(func.count()).select_from(KbFaqMiss))).scalar_one())


async def _collect_spend(deps: GenerationDeps, run_id: str) -> Spend:
    spend = Spend()
    async with deps.sessionmaker() as db:
        rows = (
            (await db.execute(select(AiCall).where(AiCall.correlation_id.like(f"eval:{run_id}:%"))))
            .scalars()
            .all()
        )
    for row in rows:
        spend.calls += 1
        spend.usd += Decimal(row.cost_usd or 0)
        spend.kzt += Decimal(row.cost_kzt or 0)
        spend.input_tokens += int(row.input_tokens or 0)
        spend.output_tokens += int(row.output_tokens or 0)
        if row.call_type is CallType.EMBEDDING:
            spend.embedding_calls += 1
    spend.usd = spend.usd.quantize(Decimal("0.000001"))
    spend.kzt = spend.kzt.quantize(Decimal("0.01"))
    return spend


async def run_suite(
    deps: GenerationDeps,
    suite: EvalSuite,
    *,
    mode: Mode,
    requests: list[ChatRequest],
    run_id: str | None = None,
    embedding_model: str = "",
) -> EvalReport:
    """Run every case through `generate_reply` — the same seam the worker
    and the sandbox use — and judge the outcome against the criteria.

    `requests` is the recording list of the chat provider in use (see
    `offline.OracleChatProvider` / `offline.RecordingChatProvider`).
    """
    run_id = run_id or uuid.uuid4().hex[:8]
    report = EvalReport(
        run_id=run_id,
        mode=mode,
        model=deps.settings.openai_chat_model,
        embedding_model=embedding_model or deps.settings.openai_embedding_model,
        started_at=datetime.now(UTC),
        notes=list(OFFLINE_NOTES if mode == "offline" else LIVE_NOTES),
    )
    for case in suite.cases:
        expect = effective_expect(case, mode)
        misses_before = await _faq_miss_count(deps)
        requests_before = len(requests)
        language = detect_language(case.input)
        outcome = await generate_reply(
            deps,
            GenerationInput(
                correlation_id=f"eval:{run_id}:{case.id}",
                question=case.input,
                language=language,
            ),
        )
        new_requests = requests[requests_before:]
        observed = Observation(
            kind=outcome.kind,
            error_code=outcome.error_code,
            text=outcome.text,
            handoff=outcome.handoff,
            model_requests=len(new_requests),
            question_forwarded=_question_forwarded(new_requests, case.input),
            answer_language=detect_language(outcome.text).value if outcome.text else None,
            detected_language=language.value,
            faq_miss=(await _faq_miss_count(deps)) > misses_before,
            escalation=escalation_reason(case.input),
            chunks=len(outcome.kb_chunk_ids),
            cost_usd=str(outcome.cost_usd),
            latency_ms=outcome.latency_ms,
        )
        failures = _judge(expect, observed)
        if language.value != case.language:
            failures.append(
                f"язык вопроса определён как `{language.value}`, ожидался `{case.language}`"
            )
        report.results.append(
            CaseResult(
                case_id=case.id,
                category=case.category,
                language=case.language,
                mandatory=case.mandatory,
                input=case.input,
                passed=not failures,
                failures=failures,
                expected=expect.model_dump(exclude_none=True),
                observed=observed,
            )
        )
    report.spend = await _collect_spend(deps, run_id)
    report.finished_at = datetime.now(UTC)
    return report


def write_report(
    report: EvalReport, out_dir: Path, *, stem: str = "eval-report"
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / f"{stem}.md"
    data = out_dir / f"{stem}.json"
    markdown.write_text(report.render_markdown(), encoding="utf-8")
    data.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return markdown, data
