"""Request/response models for auth and account endpoints.

Responses are explicit allowlists — never a dump of an ORM object
(SPEC §10: no password_hash / totp_secret / raw payloads in responses).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=200)

    phone: str = Field(min_length=5, max_length=32)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    challenge_token: str
    totp_required: bool = True


class VerifyTotpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=200)

    challenge_token: str = Field(min_length=10, max_length=128)
    code: str = Field(min_length=6, max_length=32)


class SessionUserResponse(BaseModel):
    phone: str
    full_name: str
    password_changed_at: datetime


class OkResponse(BaseModel):
    status: str = "ok"


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=200)

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class SessionInfoResponse(BaseModel):
    id: int
    user_agent: str | None
    ip: str | None
    created_at: datetime
    last_seen_at: datetime
    is_current: bool


class SessionListResponse(BaseModel):
    sessions: list[SessionInfoResponse]
