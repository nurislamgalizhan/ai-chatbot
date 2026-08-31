"""Pydantic schemas for GREEN-API wire formats.

Provider responses are UNTRUSTED input: every field access goes through a
schema (or a defensive .get()), never direct key indexing. A malformed
payload must degrade into a recorded parse failure — never into an
unhandled KeyError that kills the worker or wedges the queue.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_UNPARSED_PREVIEW_LIMIT = 2000


class NotificationEnvelope(BaseModel):
    """Outer envelope of receiveNotification.

    receipt_id is strict: without it the notification cannot be
    acknowledged at all, so a missing/broken receiptId is a protocol-level
    provider error (retry is the only option). The body is lenient: any
    non-dict body is wrapped so it can still be stored and acked.
    """

    model_config = ConfigDict(extra="ignore")

    receipt_id: int = Field(alias="receiptId")
    body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body", mode="before")
    @classmethod
    def _coerce_body(cls, value: object) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {"_unparsed": repr(value)[:_UNPARSED_PREVIEW_LIMIT]}


class SendMessageResponse(BaseModel):
    """Response of sendMessage / sendFileByUpload.

    idMessage is optional on purpose: an HTTP 200 means the provider
    accepted the message, so a missing id must NOT fail the send — failing
    it would schedule a retry and deliver the message to the customer
    twice. We record the send with an unknown provider id instead.
    """

    model_config = ConfigDict(extra="ignore")

    id_message: str | None = Field(default=None, alias="idMessage")


class InstanceStateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state_instance: str = Field(default="unknown", alias="stateInstance")
