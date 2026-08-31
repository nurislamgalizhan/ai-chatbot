"""Application configuration via pydantic-settings.

Fail-fast: invalid or missing critical configuration must prevent startup,
never fall back to a permissive default. The environment selector defaults
to the strictest mode ("prod").
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Blocklist, not a bind address: origins containing these are rejected in prod.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104  # nosec B104

# GREEN-API limit for the receiveNotification long-poll parameter.
_RECEIVE_TIMEOUT_MAX_SECONDS = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["prod", "dev", "test"] = "prod"

    database_url: str
    redis_url: str
    # Fernet key used to encrypt the TOTP secret at rest.
    encryption_key: str
    # Comma-separated list of exact origins, e.g. "https://bot.example.kz".
    cors_origins: str = ""

    session_cookie_name: str = "wabot_session"
    csrf_cookie_name: str = "wabot_csrf"
    session_idle_minutes: int = 720
    session_absolute_hours: int = 168
    login_challenge_ttl_seconds: int = 300

    login_max_attempts_phone_15m: int = 10
    login_max_attempts_ip_15m: int = 30
    lockout_threshold: int = 5
    lockout_base_minutes: int = 15

    # --- GREEN-API transport (phase 2) ---
    green_api_base_url: str = "https://api.green-api.com"
    green_api_media_url: str = "https://media.green-api.com"
    # Empty id = transport not configured; the poller idles and says so.
    green_api_id_instance: str = ""
    green_api_token: str = ""  # secret, referenced from DB as token_ref
    green_api_receive_timeout: int = 20  # long-poll seconds, 1..60
    green_api_request_timeout: float = 15.0  # + receive_timeout for polling

    retry_max_attempts: int = 3
    outbox_flush_interval_seconds: float = 1.0
    outbox_retry_base_seconds: float = 2.0

    dry_run: bool = True
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cookie_secure(self) -> bool:
        return self.app_env == "prod"

    @property
    def docs_enabled(self) -> bool:
        return self.app_env == "dev"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            msg = "DATABASE_URL must use the postgresql+asyncpg:// driver"
            raise ValueError(msg)
        return v

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:
            msg = "ENCRYPTION_KEY must be a valid Fernet key (generate_fernet_key)"
            raise ValueError(msg) from exc
        return v

    @model_validator(mode="after")
    def _validate_green_api_pair(self) -> Settings:
        if bool(self.green_api_id_instance) != bool(self.green_api_token):
            msg = (
                "GREEN_API_ID_INSTANCE and GREEN_API_TOKEN must be set together "
                "(or both left empty to disable the transport)"
            )
            raise ValueError(msg)
        if not 1 <= self.green_api_receive_timeout <= _RECEIVE_TIMEOUT_MAX_SECONDS:
            msg = "GREEN_API_RECEIVE_TIMEOUT must be within 1..60 seconds"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_cors_for_env(self) -> Settings:
        origins = self.cors_origin_list
        if self.app_env == "prod":
            if not origins:
                msg = "CORS_ORIGINS must not be empty in prod"
                raise ValueError(msg)
            for origin in origins:
                if not origin.startswith("https://"):
                    msg = f"prod CORS origin must be https: {origin!r}"
                    raise ValueError(msg)
                if any(host in origin for host in _LOCAL_HOSTS):
                    msg = f"localhost is forbidden in prod CORS origins: {origin!r}"
                    raise ValueError(msg)
        for origin in origins:
            if origin == "*" or origin.endswith("/"):
                msg = f"CORS origin must be an exact origin without trailing slash: {origin!r}"
                raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
