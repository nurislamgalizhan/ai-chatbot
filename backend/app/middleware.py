"""Security middleware: response headers, Origin allowlist, CSRF double-submit."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from app.config import Settings

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# API responses render no HTML; the CSP is a deny-all backstop.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        headers["Content-Security-Policy"] = _API_CSP
        if self._settings.app_env == "prod":
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


class CsrfAndOriginMiddleware(BaseHTTPMiddleware):
    """For mutating requests:

    - If an Origin header is present it must be in the exact allowlist.
    - If the request carries a session cookie, the X-CSRF-Token header must
      match the csrf cookie (double-submit). Requests without a session
      cookie have no ambient authority for CSRF to abuse.
    """

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings
        self._allowed_origins = frozenset(settings.cors_origin_list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None and origin not in self._allowed_origins:
                return _forbidden("Запрос с недопустимого источника")

            session_cookie = request.cookies.get(self._settings.session_cookie_name)
            if session_cookie:
                csrf_cookie = request.cookies.get(self._settings.csrf_cookie_name, "")
                csrf_header = request.headers.get("X-CSRF-Token", "")
                if not (
                    csrf_cookie and csrf_header and secrets.compare_digest(csrf_cookie, csrf_header)
                ):
                    return _forbidden("Отсутствует или неверен CSRF-токен")
        return await call_next(request)


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": detail})
