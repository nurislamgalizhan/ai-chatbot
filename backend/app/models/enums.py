"""Domain enums (rendered as native PostgreSQL enum types)."""

from __future__ import annotations

from enum import StrEnum


class ChatMode(StrEnum):
    AUTO_AI = "AUTO_AI"
    HUMAN = "HUMAN"
    PAUSED_UNTIL = "PAUSED_UNTIL"
    DISABLED = "DISABLED"


class MsgState(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    GENERATED = "GENERATED"
    SENT = "SENT"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    IGNORED = "IGNORED"
    HANDED_TO_HUMAN = "HANDED_TO_HUMAN"
    BLOCKED_BY_LIMIT = "BLOCKED_BY_LIMIT"


class MsgDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MsgSource(StrEnum):
    CUSTOMER = "customer"
    AI = "ai"
    OPERATOR_PANEL = "operator_panel"
    OPERATOR_PHONE = "operator_phone"
    SYSTEM = "system"


class MediaType(StrEnum):
    VOICE = "voice"
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"
    STICKER = "sticker"
    DOCUMENT = "document"
    OTHER = "other"


class AttentionReason(StrEnum):
    HANDOFF = "handoff"
    PROCESSING_ERROR = "processing_error"
    BLOCKED_BY_LIMIT = "blocked_by_limit"
    MANUAL = "manual"


class LastActor(StrEnum):
    AI = "ai"
    HUMAN = "human"
    CUSTOMER = "customer"


class AttentionClearedReason(StrEnum):
    OPERATOR_PHONE_REPLY = "operator_phone_reply"
    PANEL_OPENED = "panel_opened"
    MANUAL = "manual"


class KbLanguage(StrEnum):
    RU = "ru"
    KK = "kk"
    EN = "en"


class AnswerStatus(StrEnum):
    """A machine draft is never delivered to a customer (owner decision)."""

    ABSENT = "absent"
    MACHINE = "machine"
    APPROVED = "approved"


class DocStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class SourceType(StrEnum):
    MANUAL_TEXT = "manual_text"
    FILE = "file"
    FAQ = "faq"
