"""Brute-force protection: progressive lockout and login rate limits."""

from __future__ import annotations

from tests.conftest import TEST_PASSWORD


async def test_lockout_after_repeated_failures(client, settings, admin_factory) -> None:
    admin = await admin_factory()

    for _ in range(settings.lockout_threshold):
        response = await client.post(
            "/api/auth/login",
            json={"phone": admin.phone, "password": "wrong-password-123"},
        )
        assert response.status_code == 401

    # Locked now: even the CORRECT password is rejected — with the SAME
    # 401 a wrong password gets, so the lock does not reveal the account.
    response = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    assert response.status_code == 401


async def test_ip_rate_limit_on_login(client, settings, admin_factory) -> None:
    """Attempts against a nonexistent phone: no account to lock, so the
    Redis rate limiter is the only thing standing — it must trip."""
    await admin_factory()
    statuses = []
    for _ in range(settings.login_max_attempts_phone_15m + 1):
        response = await client.post(
            "/api/auth/login",
            json={"phone": "+77015550000", "password": "wrong-password-123"},
        )
        statuses.append(response.status_code)
    assert statuses[-1] == 429
    assert all(s == 401 for s in statuses[:-1])
