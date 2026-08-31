"""Structured JSON logging with correlation ids and secret redaction."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from app.config import Settings

_SENSITIVE_KEY_RE = re.compile(r"password|token|secret|authorization|cookie|api_key", re.IGNORECASE)
_PHONE_RE = re.compile(r"\+\d{6,14}\d")

REQUEST_ID_HEADER = "X-Request-ID"


def mask_phone(phone: str) -> str:
    """+77019481555 -> +7701***1555 — never log full phone numbers."""
    if len(phone) < 9:  # noqa: PLR2004 - too short to be a real phone, mask fully
        return "***"
    return f"{phone[:5]}***{phone[-4:]}"


def _redact(_: object, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if _SENSITIVE_KEY_RE.search(key):
            event_dict[key] = "[redacted]"
        elif isinstance(value, str) and _PHONE_RE.fullmatch(value):
            event_dict[key] = mask_phone(value)
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id to every request; echo it back in the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        correlation_id = incoming if _is_safe_request_id(incoming) else uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id")
        response.headers[REQUEST_ID_HEADER] = correlation_id
        return response


def _is_safe_request_id(value: str) -> bool:
    return bool(value) and len(value) <= 64 and value.replace("-", "").isalnum()  # noqa: PLR2004
