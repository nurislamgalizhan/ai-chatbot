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
    # Terminal state of an INBOUND customer message once its reply exists;
    # SENT stays the terminal state of outbound messages only.
    ANSWERED = "ANSWERED"
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


class CallType(StrEnum):
    CHAT = "chat"
    TRANSCRIPTION = "transcription"
    EMBEDDING = "embedding"
    VISION = "vision"


class AnswerSource(StrEnum):
    """How an outgoing AI answer was produced.

    The only record that a FREE answer happened: FAQ and cache hits
    return before any ai_calls row exists, so without this they could
    not be counted at all.
    """

    MODEL = "model"
    FAQ = "faq"
    CACHE = "cache"
    #: A refusal, handoff or file acknowledgement composed by the model
    #: (or the built-in safety net when it could not be called).
    SERVICE = "service"


class MediaJobState(StrEnum):
    """Lifecycle of one media file's pre-processing (phase 7).

    Kept apart from msg_state: a job can fail without the MESSAGE being
    a failure — it just becomes a human's problem instead.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    # Deliberately not analyzed: sensitive content, a limit, a kill
    # switch. Terminal and free.
    REFUSED = "refused"
    FAILED = "failed"


class BudgetScope(StrEnum):
    GLOBAL_DAY = "global_day"
    GLOBAL_MONTH = "global_month"
    CHAT = "chat"
    USER_DAY = "user_day"


class PriceUnit(StrEnum):
    PER_1M_INPUT = "per_1m_input"
    PER_1M_CACHED_INPUT = "per_1m_cached_input"
    PER_1M_OUTPUT = "per_1m_output"
    PER_MINUTE_AUDIO = "per_minute_audio"


class ReservationStatus(StrEnum):
    HELD = "held"
    SETTLED = "settled"
    RELEASED = "released"
