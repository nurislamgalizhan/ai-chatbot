"""Shared fixtures. Race-sensitive and constraint tests run against a real
PostgreSQL (testcontainers), never SQLite (SPEC §17)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pyotp
import pytest
from cryptography.fernet import Fernet

# Must be set before importing app modules that read settings.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CORS_ORIGINS", "")

import httpx
from asgi_lifespan import LifespanManager
from fakeredis.aioredis import FakeRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.main import create_app
from app.services.admin_setup import CreatedAdmin, create_admin

if TYPE_CHECKING:
    from fastapi import FastAPI

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"

TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PHONE = "+77010000001"

APP_TABLES = (
    "audit_log",
    "login_challenges",
    "admin_sessions",
    "admin_totp_recovery_codes",
    "admin_user",
)


@pytest.fixture(scope="session")
def database_url() -> AsyncIterator[str]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield external
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture
def settings(migrated_db: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=migrated_db,
        redis_url="redis://unused:6379/0",
        encryption_key=os.environ["ENCRYPTION_KEY"],
        cors_origins="",
    )


@pytest.fixture
async def clean_db(migrated_db: str) -> AsyncIterator[str]:
    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    yield migrated_db


@pytest.fixture
async def db_engine(clean_db: str) -> AsyncIterator:
    engine = create_async_engine(clean_db)
    yield engine
    await engine.dispose()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
async def app(settings: Settings, clean_db: str, redis: FakeRedis) -> AsyncIterator[FastAPI]:
    application = create_app(settings, redis=redis)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def admin_factory(clean_db: str, settings: Settings) -> Callable[..., Awaitable[CreatedAdmin]]:
    async def _create(
        phone: str = TEST_PHONE,
        password: str = TEST_PASSWORD,
        full_name: str = "Test Admin",
    ) -> CreatedAdmin:
        engine = create_async_engine(clean_db)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db, db.begin():
                return await create_admin(db, settings, phone, full_name, password)
        finally:
            await engine.dispose()

    return _create


def totp_code(secret: str, offset_steps: int = 0) -> str:
    """A valid TOTP code; offset_steps=-1 gives the previous window's code,
    letting one test authenticate twice without tripping replay protection."""
    at = datetime.now(UTC) + timedelta(seconds=30 * offset_steps)
    return pyotp.TOTP(secret).at(at)


async def login(
    client: httpx.AsyncClient,
    settings: Settings,
    admin: CreatedAdmin,
    password: str = TEST_PASSWORD,
    offset_steps: int = 0,
) -> str:
    """Full login flow; returns the CSRF token for mutating requests."""
    step1 = await client.post("/api/auth/login", json={"phone": admin.phone, "password": password})
    assert step1.status_code == 200, step1.text
    step2 = await client.post(
        "/api/auth/verify-totp",
        json={
            "challenge_token": step1.json()["challenge_token"],
            "code": totp_code(admin.totp_secret, offset_steps),
        },
    )
    assert step2.status_code == 200, step2.text
    csrf = client.cookies.get(settings.csrf_cookie_name)
    assert csrf
    return csrf
