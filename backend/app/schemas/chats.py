"""Request/response models for the chat manager. Responses are explicit
allowlists; requests forbid extra fields (no mass assignment)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TagResponse(BaseModel):
    id: int
    name: str
    color: str | None


class ChatListItem(BaseModel):
    id: int
    phone: str
    display_name: str | None
    mode: str
    paused_until: datetime | None
    is_resolved: bool
    unread_count: int
    needs_attention: bool
    attention_reason: str | None
    last_message_at: datetime
    last_actor: str | None
    last_message_preview: str | None
    total_cost_usd: float
    tags: list[TagResponse]


class ChatListResponse(BaseModel):
    items: list[ChatListItem]
    next_cursor: str | None


class ChatDetailResponse(BaseModel):
    id: int
    phone: str
    display_name: str | None
    wa_chat_id: str
    mode: str
    paused_until: datetime | None
    is_resolved: bool
    needs_attention: bool
    attention_reason: str | None
    last_message_at: datetime
    total_cost_usd: float
    created_at: datetime
    tags: list[TagResponse]


class MessageResponse(BaseModel):
    id: int
    direction: str
    source: str
    body: str | None
    media_type: str | None
    transcript: str | None
    state: str
    delivery_status: str | None
    error_code: str | None
    created_at: datetime
    sent_at: datetime | None


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)
    # Client-generated idempotency key: a retried request cannot double-send.
    client_message_id: uuid.UUID


class SendMessageResponse(BaseModel):
    message_id: int
    created: bool


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_resolved: bool


class AttentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_attention: bool


class NoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)


class NoteResponse(BaseModel):
    id: int
    body: str
    created_at: datetime


class NoteListResponse(BaseModel):
    items: list[NoteResponse]


class TagCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    color: str | None = Field(default=None, max_length=20)


class TagListResponse(BaseModel):
    items: list[TagResponse]


class AttachTagRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: int
