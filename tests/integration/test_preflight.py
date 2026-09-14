"""Preflight tells the owner what is missing before DRY_RUN goes off."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.eval.preflight import run_preflight
from app.models import FxRate
from app.services.agent import activate_version, create_version
from app.services.pricing import seed_default_prices


def _by_name(report):
    return {check.name: check for check in report.checks}


async def test_empty_installation_is_reported_as_not_ready(db_engine, settings) -> None:
    report = await run_preflight(settings, redis=FakeRedis())
    checks = _by_name(report)
    assert not report.ok
    assert checks["Учётная запись"].status == "fail"
    assert checks["Инструкция агента"].status == "fail"
    assert checks["Цены моделей"].status == "fail"
    assert checks["Курс USD/KZT"].status == "fail"
    assert checks["Миграции"].status == "ok"
    assert checks["База данных"].status == "ok"
    assert checks["Redis"].status == "ok"
    assert checks["Воркер"].status == "warn"  # no heartbeat in the fake
    assert checks["GREEN-API"].status == "warn"  # not configured in tests
    assert "Настройки инстанса" not in checks  # nothing to ask the provider about
    markdown = report.render_markdown()
    assert "есть блокирующие проблемы" in markdown


async def test_seeded_installation_passes_the_blocking_checks(
    db_engine, settings, admin_factory
) -> None:
    await admin_factory()
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        db.add(FxRate(pair="USD/KZT", rate=Decimal(500), effective_from=datetime.now(UTC)))
        await seed_default_prices(db, settings)
        instruction = await create_version(
            db,
            system_prompt="Ты — консультант организации.",
            style=None,
            escalation_rules=None,
            max_output_tokens=None,
            reasoning_effort=None,
            author_user_id=None,
        )
        await activate_version(db, instruction, actor_user_id=None)
    redis = FakeRedis()
    await redis.set("worker:heartbeat", "1")

    report = await run_preflight(settings, redis=redis)
    checks = _by_name(report)
    assert report.ok, [c for c in report.checks if c.status == "fail"]
    assert checks["Учётная запись"].status == "ok"
    assert checks["Воркер"].status == "ok"
    # Honest warnings for what the owner still has to do.
    assert checks["Справка для бота"].status == "warn"
    assert checks["Оповещения владельцу"].status == "warn"
    assert checks["GREEN-API"].status == "warn"
    assert "можно запускать" in report.render_markdown()
