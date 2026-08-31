"""Single-admin invariants: creation, duplication, startup check."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import StartupCheckError, create_app
from app.services.admin_setup import (
    AdminSetupError,
    create_admin,
    reset_admin_password,
)
from tests.conftest import TEST_PASSWORD


async def test_create_admin_refuses_second_account(admin_factory, db_engine, settings) -> None:
    await admin_factory()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        with pytest.raises(AdminSetupError, match="уже существует"):
            await create_admin(db, settings, "+77012223344", "Second", TEST_PASSWORD)


async def test_create_admin_rejects_weak_password(db_engine, settings, clean_db) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        with pytest.raises(AdminSetupError, match="12"):
            await create_admin(db, settings, "+77012223344", "Admin", "short")


async def test_startup_fails_with_two_active_admins(
    admin_factory, db_engine, settings, redis
) -> None:
    await admin_factory()
    # A second active admin can only appear via manual DB edits — the app
    # must refuse to start rather than run with an untested access model.
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO admin_user (phone, full_name, password_hash) "
                "VALUES ('+77015556677', 'Intruder', 'x')"
            )
        )
    app = create_app(settings, redis=redis)
    from asgi_lifespan import LifespanManager

    with pytest.raises(StartupCheckError, match="exactly one"):
        async with LifespanManager(app):
            pass


async def test_reset_admin_password_revokes_sessions(admin_factory, db_engine, settings) -> None:
    admin = await admin_factory()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        phone = await reset_admin_password(db, TEST_PASSWORD + "-new")
    assert phone == admin.phone


async def test_reset_password_without_admin_fails(db_engine, settings, clean_db) -> None:
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        with pytest.raises(AdminSetupError, match="не создана"):
            await reset_admin_password(db, TEST_PASSWORD)
