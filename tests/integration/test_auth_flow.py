"""End-to-end login flow: password -> challenge -> TOTP -> session."""

from __future__ import annotations

import httpx

from tests.conftest import TEST_PASSWORD, login, totp_code


async def test_full_login_grants_access(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    await login(client, settings, admin)

    response = await client.get("/api/account")
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == admin.phone
    # Explicit response model: no secrets can appear.
    assert "password_hash" not in body
    assert "totp_secret_encrypted" not in body


async def test_unknown_phone_and_wrong_password_are_indistinguishable(
    client, admin_factory
) -> None:
    await admin_factory(phone="+77010000001")
    unknown = await client.post(
        "/api/auth/login",
        json={"phone": "+77019999999", "password": "whatever-wrong-1"},
    )
    wrong = await client.post(
        "/api/auth/login",
        json={"phone": "+77010000001", "password": "whatever-wrong-1"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_challenge_token_is_not_a_session(client, settings, admin_factory) -> None:
    """Intermediate 'password accepted' state must grant zero API access."""
    admin = await admin_factory()
    step1 = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    assert step1.status_code == 200
    challenge_token = step1.json()["challenge_token"]

    client.cookies.set(settings.session_cookie_name, challenge_token, path="/api")
    response = await client.get("/api/account")
    assert response.status_code == 401


async def test_wrong_totp_rejected(client, admin_factory) -> None:
    admin = await admin_factory()
    step1 = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    response = await client.post(
        "/api/auth/verify-totp",
        json={"challenge_token": step1.json()["challenge_token"], "code": "000000"},
    )
    assert response.status_code == 401
    assert not client.cookies.get("wabot_session")


async def test_totp_replay_rejected(app, settings, admin_factory) -> None:
    """The same TOTP code cannot complete two logins."""
    admin = await admin_factory()
    code = totp_code(admin.totp_secret)
    transport = httpx.ASGITransport(app=app)

    async def attempt() -> int:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            step1 = await c.post(
                "/api/auth/login",
                json={"phone": admin.phone, "password": TEST_PASSWORD},
            )
            assert step1.status_code == 200
            step2 = await c.post(
                "/api/auth/verify-totp",
                json={
                    "challenge_token": step1.json()["challenge_token"],
                    "code": code,
                },
            )
            return step2.status_code

    assert await attempt() == 200
    assert await attempt() == 401


async def test_challenge_single_use(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    step1 = await client.post(
        "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
    )
    challenge = step1.json()["challenge_token"]
    ok = await client.post(
        "/api/auth/verify-totp",
        json={"challenge_token": challenge, "code": totp_code(admin.totp_secret)},
    )
    assert ok.status_code == 200
    # A fresh client (attacker) replaying the consumed challenge token.
    client.cookies.clear()
    replay = await client.post(
        "/api/auth/verify-totp",
        json={
            "challenge_token": challenge,
            "code": totp_code(admin.totp_secret, offset_steps=-1),
        },
    )
    assert replay.status_code == 401


async def test_recovery_code_works_once(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    recovery_code = admin.recovery_codes[0]

    async def login_with_recovery() -> int:
        step1 = await client.post(
            "/api/auth/login", json={"phone": admin.phone, "password": TEST_PASSWORD}
        )
        assert step1.status_code == 200
        step2 = await client.post(
            "/api/auth/verify-totp",
            json={
                "challenge_token": step1.json()["challenge_token"],
                "code": recovery_code,
            },
        )
        return step2.status_code

    assert await login_with_recovery() == 200
    client.cookies.clear()
    assert await login_with_recovery() == 401


async def test_logout_revokes_session(client, settings, admin_factory) -> None:
    admin = await admin_factory()
    csrf = await login(client, settings, admin)
    session_cookie = client.cookies.get(settings.session_cookie_name)

    response = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200

    # Even if the browser kept the cookie, the server-side session is dead.
    client.cookies.set(settings.session_cookie_name, session_cookie, path="/api")
    assert (await client.get("/api/account")).status_code == 401


async def test_invalid_phone_format_gets_same_401(client, admin_factory) -> None:
    await admin_factory()
    response = await client.post(
        "/api/auth/login", json={"phone": "not-a-phone", "password": "x" * 12}
    )
    assert response.status_code == 401
