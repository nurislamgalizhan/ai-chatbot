"""What the provider reports as usage — the input to every money number.

The formula was always right; what was wrong was the numbers handed to
it. OpenAI's `completion_tokens` ALREADY contains the reasoning tokens,
so storing it whole and then pricing (output + reasoning) charged the
reasoning twice: every answer looked dearer than it was, the ledger
drifted from reality, and the panel showed inflated dialog costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from app.ai.chat_provider import ChatError, ChatRequest, OpenAIChatProvider
from app.services.pricing import PriceSheet, Usage, compute_cost_usd

PRICES = PriceSheet(
    model="m",
    input_usd=Decimal("0.20"),
    cached_input_usd=Decimal("0.02"),
    output_usd=Decimal("1.20"),
)


@dataclass
class _Settings:
    openai_api_key: str = "test-key"
    openai_request_timeout: float = 5.0


class _Response:
    """Shaped like the OpenAI SDK's completion object."""

    def __init__(self, *, text: str, completion: int, reasoning: int) -> None:
        self.choices = [
            type("Choice", (), {"message": type("Msg", (), {"content": text})()})()
        ]
        self.model = "gpt-5.6-luna"
        self.usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": 1000,
                "completion_tokens": completion,  # includes reasoning
                "prompt_tokens_details": type("P", (), {"cached_tokens": 400})(),
                "completion_tokens_details": type(
                    "C", (), {"reasoning_tokens": reasoning}
                )(),
            },
        )()


def _provider(response: _Response) -> OpenAIChatProvider:
    provider = OpenAIChatProvider(_Settings())

    class _Completions:
        @staticmethod
        async def create(**_kwargs):
            return response

    provider._client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": _Completions()})()},
    )()
    return provider


REQUEST = ChatRequest(
    system="s",
    messages=[{"role": "user", "content": "q"}],
    model="gpt-5.6-luna",
    max_output_tokens=350,
    reasoning_effort="low",
)


async def test_reasoning_tokens_are_not_billed_twice() -> None:
    """500 completion tokens of which 200 were reasoning must be priced
    as 500 output tokens — not 700."""
    provider = _provider(_Response(text="ответ", completion=500, reasoning=200))

    result = await provider.complete(REQUEST)

    assert result.usage.output_tokens == 300  # what the customer sees
    assert result.usage.reasoning_tokens == 200
    # The two halves still add up to exactly what the provider billed.
    assert result.usage.output_tokens + result.usage.reasoning_tokens == 500

    cost = compute_cost_usd(PRICES, Usage(**vars(result.usage)))
    expected = (
        Decimal(600) * Decimal("0.20")  # 1000 prompt - 400 cached
        + Decimal(400) * Decimal("0.02")
        + Decimal(500) * Decimal("1.20")  # completion tokens, once
    ) / Decimal(1_000_000)
    assert cost == expected.quantize(Decimal("0.00000001"))


async def test_usage_without_reasoning_is_unchanged() -> None:
    provider = _provider(_Response(text="ответ", completion=120, reasoning=0))

    result = await provider.complete(REQUEST)

    assert result.usage.output_tokens == 120
    assert result.usage.reasoning_tokens == 0


async def test_empty_answer_reports_the_same_split() -> None:
    """A billed-but-empty answer must carry the same corrected numbers,
    since it is accounted for like any other spend."""
    provider = _provider(_Response(text="   ", completion=400, reasoning=400))

    with pytest.raises(ChatError) as excinfo:
        await provider.complete(REQUEST)

    usage = excinfo.value.usage
    assert usage is not None
    assert usage.output_tokens == 0  # all of it went to thinking
    assert usage.reasoning_tokens == 400
