"""Every registered route except the explicit whitelist must answer 401
without a session. Walks app.routes automatically so that a new router
added without require_admin makes this test fail (SPEC §10, §17)."""

from __future__ import annotations

import re
from collections.abc import Iterator

from fastapi.routing import APIRoute

# The complete inventory of unauthenticated routes in the system.
WHITELIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/healthz"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/verify-totp"),
    }
)


def iter_api_routes(routes: list[object]) -> Iterator[APIRoute]:
    """Recursively walk nested routers: FastAPI wraps each included router
    into an _IncludedRouter that exposes the APIRouter as original_router."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner_router = getattr(route, "original_router", None)
        if inner_router is not None:
            yield from iter_api_routes(inner_router.routes)
        nested = getattr(route, "routes", None)
        if nested:
            yield from iter_api_routes(nested)


async def test_every_route_requires_session(app, client) -> None:
    checked = 0
    seen: set[tuple[str, str]] = set()
    for route in iter_api_routes(app.routes):
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in seen:
                continue
            seen.add((method, route.path))
            if (method, route.path) in WHITELIST:
                continue
            url = re.sub(r"\{[^}]+\}", "1", route.path)
            response = await client.request(method, url)
            assert response.status_code == 401, (
                f"{method} {route.path} answered {response.status_code} without a "
                "session — a router was registered without require_admin"
            )
            checked += 1
    # Sanity: the walk found real protected endpoints, not an empty app.
    assert checked >= 5


async def test_healthz_is_open_and_reveals_nothing(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_docs_disabled_outside_dev(client) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404
