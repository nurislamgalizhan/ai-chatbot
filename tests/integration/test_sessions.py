"""Session lifecycle: expiry, idle timeout, revocation, tampering, CSRF."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text

from tests.conftest import TEST_PASSWORD, login


async def _age_sessions(db_engine, **columns: datetime) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in columns)
    async with db_engine.begin() as conn:
        await conn.execute(text(f"UPDATE admin_sessions SET {sets}"), columns)


async def test_tampered_token_is_401_not_500(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    await login(client, settings, admin)
    client.cookies.set(settings.session_cookie_name, "forged-token-value", path="/api")
    response = await client.get("/api/account")
    assert response.status_code == 401


async def test_expired_session_is_401(
    client, settings, admin_factory, db_engine
) -> None:
    admin = await admin_factory()
    await login(client, settings, admin)
    await _age_sessions(db_engine, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert (await client.get("/api/account")).status_code == 401


async def test_idle_timeout_is_401(client, settings, admin_factory, db_engine) -> None:
    admin = await admin_factory()
    await login(client, settings, admin)
    stale = datetime.now(UTC) - timedelta(minutes=settings.session_idle_minutes + 1)
    await _age_sessions(db_engine, last_seen_at=stale)
    assert (await client.get("/api/account")).status_code == 401


async def test_csrf_required_for_mutating_requests(
    client, settings, admin_factory
) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)

    without = await client.post("/api/auth/logout")
    assert without.status_code == 403

    with_token = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert with_token.status_code == 200


async def test_origin_allowlist_enforced(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    response = await client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


async def test_password_change_revokes_other_sessions(
    app, settings, admin_factory
) -> None:
    admin = await admin_factory()
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as first,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as second,
    ):
        # Different TOTP windows so replay protection is not tripped.
        await login(first, settings, admin, offset_steps=-1)
        csrf_second = await login(second, settings, admin)

        new_password = TEST_PASSWORD + "-rotated"
        response = await second.post(
            "/api/account/change-password",
            json={"current_password": TEST_PASSWORD, "new_password": new_password},
            headers={"X-CSRF-Token": csrf_second},
        )
        assert response.status_code == 200

        # The session that changed the password survives...
        assert (await second.get("/api/account")).status_code == 200
        # ...every other session is revoked.
        assert (await first.get("/api/account")).status_code == 401


async def test_change_password_requires_current(
    client, settings, admin_factory
) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    response = await client.post(
        "/api/account/change-password",
        json={"current_password": "guess-wrong-123", "new_password": "x" * 16},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403


async def test_mass_assignment_rejected(client, settings, admin_factory) -> None:
    """Extra fields like password_hash / is_active must be rejected, not
    silently bound (schemas use extra='forbid')."""
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    response = await client.post(
        "/api/account/change-password",
        json={
            "current_password": TEST_PASSWORD,
            "new_password": "x" * 16,
            "password_hash": "evil",
            "is_active": False,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422


async def test_session_list_and_targeted_revoke(
    client, settings, admin_factory
) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)

    listing = await client.get("/api/account/sessions")
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["is_current"] is True

    # Nonexistent id -> 404, not 500 and not a silent no-op.
    missing = await client.post(
        "/api/account/sessions/999999/revoke", headers={"X-CSRF-Token": csrf}
    )
    assert missing.status_code == 404
