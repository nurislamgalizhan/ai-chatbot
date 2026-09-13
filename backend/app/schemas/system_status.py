"""Response shapes for «Система» and the account journal.

Nothing here carries a secret or a customer's words: the status page is
the one screen an owner screenshots and sends to a contractor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TransportStatus(BaseModel):
    configured: bool
    instance_id: str
    #: "••••1234" — the last four characters, never more (SPEC §12.9).
    token_hint: str
    last_polled_at: datetime | None
    last_error: str | None
    state: str | None


class QueueStatus(BaseModel):
    queue_depth: int
    oldest_queued_minutes: int
    inbox_backlog: int
    outbox_pending: int
    failed_24h: int


class DiskStatus(BaseModel):
    used_percent: int
    free_gb: float
    total_gb: float
    database_mb: float


class BackupStatus(BaseModel):
    last_at: datetime | None
    age_hours: int | None
    stale_after_hours: int


class AlertRow(BaseModel):
    id: int
    kind: str
    event_key: str
    body: str
    status: str
    created_at: datetime
    sent_at: datetime | None
    last_error: str | None


class AlertsStatus(BaseModel):
    enabled: bool
    phone_hint: str
    pending: int
    failed: int
    recent: list[AlertRow] = Field(default_factory=list)


class SystemStatusResponse(BaseModel):
    dry_run: bool
    emergency_ai_off: bool
    transport: TransportStatus
    model: str
    openai_configured: bool
    queues: QueueStatus
    disk: DiskStatus
    backup: BackupStatus
    alerts: AlertsStatus


class SystemEventRow(BaseModel):
    id: int
    level: str
    component: str
    event: str
    detail: dict[str, Any]
    created_at: datetime


class SystemEventsResponse(BaseModel):
    items: list[SystemEventRow] = Field(default_factory=list)


class TestAlertResponse(BaseModel):
    """What pressing «Отправить тестовое оповещение» actually achieved."""

    queued: bool
    will_send: bool
    #: "no_phone" | "alerts_off" | None
    reason: str | None = None


class ConnectionCheckResponse(BaseModel):
    ok: bool
    state: str | None
    error: str | None


class AuditRow(BaseModel):
    id: int
    action: str
    entity_type: str | None
    entity_id: str | None
    ip: str | None
    created_at: datetime
    #: Kept deliberately shallow: the journal says WHAT changed, not the
    #: full before/after of settings that may hold phone numbers.
    summary: str


class AuditResponse(BaseModel):
    items: list[AuditRow] = Field(default_factory=list)
    next_cursor: int | None = None


class LoginAttemptRow(BaseModel):
    id: int
    created_at: datetime
    ip: str | None
    user_agent: str | None
    outcome: str


class LoginHistoryResponse(BaseModel):
    items: list[LoginAttemptRow] = Field(default_factory=list)
    last_success_at: datetime | None = None


class ConfirmPasswordRequest(BaseModel):
    """Re-authentication for an action that outlives the session."""

    current_password: str = Field(min_length=1, max_length=200)


class RecoveryCodesResponse(BaseModel):
    """Shown once and never again: only hashes are kept."""

    codes: list[str]


class RecoveryCodesStatusResponse(BaseModel):
    unused: int
    total: int
