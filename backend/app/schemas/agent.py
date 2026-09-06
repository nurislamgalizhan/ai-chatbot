"""Schemas for the agent instruction editor and the sandbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

_EFFORT_PATTERN = "^(none|low|medium|high|xhigh|max)$"


class InstructionPayload(BaseModel):
    system_prompt: str = Field(min_length=20, max_length=20_000)
    style: str | None = Field(default=None, max_length=4_000)
    # {"handoff": {"ru": ..., "kk": ..., "en": ...}, "offtopic": {...}}
    fallback_templates: dict[str, dict[str, str]] = Field(default_factory=dict)
    escalation_rules: dict[str, Any] | None = None
    max_output_tokens: int | None = Field(default=None, ge=50, le=2_000)
    reasoning_effort: str | None = Field(default=None, pattern=_EFFORT_PATTERN)


class InstructionItem(BaseModel):
    id: int
    version: int
    is_active: bool
    system_prompt: str
    style: str | None
    fallback_templates: dict[str, Any]
    escalation_rules: dict[str, Any] | None
    max_output_tokens: int | None
    reasoning_effort: str | None
    created_at: datetime


class InstructionListResponse(BaseModel):
    items: list[InstructionItem]
    active_version: int | None


class SandboxRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)


class SandboxUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


class SandboxResponse(BaseModel):
    kind: str
    text: str | None
    error_code: str | None
    cost_usd: str
    cost_kzt: str
    usage: SandboxUsage | None
    latency_ms: int | None
    kb_chunk_ids: list[int]
    instruction_version: int | None
    model: str | None
    detected_language: str
