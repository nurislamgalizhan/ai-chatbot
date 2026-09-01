"""Knowledge-base request/response models. Requests forbid extra fields;
responses are explicit allowlists."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Language = Literal["ru", "kk", "en"]
DocumentStatus = Literal["draft", "active", "archived"]
AnswerStatusValue = Literal["absent", "machine", "approved"]


class DocumentListItem(BaseModel):
    id: int
    title: str
    category: str | None
    language: Language
    status: DocumentStatus
    version: int
    is_searchable: bool
    valid_until: datetime | None
    index_state: str
    index_error: str | None
    chunk_count: int
    updated_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentListItem]


class DocumentDetail(DocumentListItem):
    body: str
    created_at: datetime


class DocumentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)
    category: str | None = Field(default=None, max_length=60)
    language: Language = "ru"
    status: DocumentStatus = "draft"
    is_searchable: bool = True
    valid_until: datetime | None = None


class DocumentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=200_000)
    category: str | None = Field(default=None, max_length=60)
    language: Language | None = None
    status: DocumentStatus | None = None
    is_searchable: bool | None = None
    valid_until: datetime | None = None


class ChunkItem(BaseModel):
    id: int
    chunk_index: int
    content: str
    token_count: int
    embedding_model: str


class ChunkListResponse(BaseModel):
    items: list[ChunkItem]


class ProcessingLogItem(BaseModel):
    step: str
    status: str
    detail: str | None
    tokens_used: int
    created_at: datetime


class ProcessingLogResponse(BaseModel):
    items: list[ProcessingLogItem]


class FaqPatternItem(BaseModel):
    id: int
    language: Language
    pattern: str


class FaqItem(BaseModel):
    id: int
    normalized_key: str
    category: str | None
    is_active: bool
    answer_ru: str | None
    answer_ru_status: AnswerStatusValue
    answer_kk: str | None
    answer_kk_status: AnswerStatusValue
    answer_en: str | None
    answer_en_status: AnswerStatusValue
    patterns: list[FaqPatternItem]
    updated_at: datetime


class FaqListResponse(BaseModel):
    items: list[FaqItem]


class FaqCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_key: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_-]+$")
    category: str | None = Field(default=None, max_length=60)
    answer_ru: str = Field(min_length=1, max_length=4000)
    patterns_ru: list[str] = Field(min_length=1, max_length=20)
    patterns_kk: list[str] = Field(min_length=1, max_length=20)
    patterns_en: list[str] = Field(default_factory=list, max_length=20)


class FaqAnswerUpdateRequest(BaseModel):
    """Editing one language of one entry. `approved` may only be set on
    a text a human has in front of them — mass approval is forbidden by
    the owner's policy, so this endpoint takes a single entry."""

    model_config = ConfigDict(extra="forbid")

    language: Language
    answer: str | None = Field(default=None, max_length=4000)
    status: AnswerStatusValue


class FaqActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


class FaqPatternCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language
    pattern: str = Field(min_length=2, max_length=200)


class TranslationReviewItem(BaseModel):
    """One row of the translation review screen: the ru source next to
    the draft awaiting approval."""

    faq_id: int
    normalized_key: str
    category: str | None
    source_ru: str | None
    language: Language
    draft: str | None
    status: AnswerStatusValue
    miss_count: int


class TranslationReviewResponse(BaseModel):
    items: list[TranslationReviewItem]


class LanguageMissRow(BaseModel):
    language: Language
    faq_id: int | None
    normalized_key: str | None
    category: str | None
    misses: int


class LanguageMissResponse(BaseModel):
    """Analytics: how many questions per language fell through to LLM
    because no approved answer existed."""

    items: list[LanguageMissRow]
    total: int


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    category: str | None = Field(default=None, max_length=60)


class SearchHit(BaseModel):
    chunk_id: int
    document_id: int
    title: str
    content: str
    score: float


class SearchResponse(BaseModel):
    """Admin-side retrieval preview: shows exactly what RAG would feed
    the model — no OpenAI chat call is made."""

    faq_hit: str | None
    faq_language_miss: bool
    detected_language: Language
    hits: list[SearchHit]
