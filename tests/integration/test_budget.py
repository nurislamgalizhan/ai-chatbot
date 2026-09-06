"""Money: atomic reservations, settle/release, sweeper, thresholds.

The mandatory SPEC §9.2 race test lives here: 20 parallel reservations
against a limit that fits 4 must let exactly 4 through — sequential tests
cannot catch this bug, so everything runs against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.models import AlertNotification, BudgetLedger, BudgetReservation, ModelPrice
from app.models.enums import BudgetScope, PriceUnit, ReservationStatus
from app.services import app_settings, budget
from app.services.app_settings import set_setting
from app.services.pricing import (
    PriceSheet,
    PricingError,
    Usage,
    compute_cost_usd,
    estimate_max_cost_usd,
    load_price_sheet,
    load_usd_kzt_rate,
    seed_default_prices,
    usd_to_kzt,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

RATE = Decimal("500")  # 1 USD = 500 KZT for readable numbers


async def _seed_rate(maker: async_sessionmaker, rate: Decimal = RATE) -> None:
    from app.models import FxRate

    async with maker() as db, db.begin():
        db.add(FxRate(pair="USD/KZT", rate=rate, effective_from=datetime.now(UTC)))


async def _set_limits_kzt(
    maker: async_sessionmaker,
    *,
    day: Decimal | int = 10_000,
    month: Decimal | int = 100_000,
    dialog: Decimal | int = 10_000,
    user_day: Decimal | int = 10_000,
) -> None:
    async with maker() as db, db.begin():
        await set_setting(db, app_settings.DAILY_AI_BUDGET_KZT, float(day))
        await set_setting(db, app_settings.MONTHLY_AI_BUDGET_KZT, float(month))
        await set_setting(db, app_settings.MAX_AI_COST_PER_DIALOG_KZT, float(dialog))
        await set_setting(db, app_settings.MAX_AI_COST_PER_USER_PER_DAY_KZT, float(user_day))


def _maker(db_engine) -> async_sessionmaker:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _day_ledger(maker: async_sessionmaker) -> BudgetLedger:
    async with maker() as db:
        return (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one()


async def test_twenty_parallel_reservations_limit_four(db_engine) -> None:
    """SPEC §9.2 mandatory test: the limit fits exactly 4 estimates; 20
    parallel attempts (distinct chats/phones so only the global day scope
    is contended) -> exactly 4 allowed, spent+reserved never over limit."""
    maker = _maker(db_engine)
    await _seed_rate(maker)
    # Day limit: 4 estimates of $0.01 = $0.04 = 20 KZT at rate 500.
    await _set_limits_kzt(maker, day=20)
    estimate = Decimal("0.010000")

    async def attempt(i: int) -> bool:
        outcome = await budget.reserve(
            maker,
            sub_correlation=f"corr-{i}:main",
            estimate_usd=estimate,
            chat_id=i + 1,
            phone=f"+7701000{i:04d}",
        )
        return outcome.allowed

    results = await asyncio.gather(*(attempt(i) for i in range(20)))
    assert sum(results) == 4

    ledger = await _day_ledger(maker)
    assert ledger.reserved_usd + ledger.spent_usd <= ledger.limit_usd
    assert ledger.reserved_usd == estimate * 4


async def test_settle_books_actual_and_is_idempotent(db_engine) -> None:
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker)
    outcome = await budget.reserve(
        maker,
        sub_correlation="c1:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    assert outcome.allowed

    actual = Decimal("0.003000")
    assert await budget.settle(maker, sub_correlation="c1:main", actual_usd=actual) == 4
    ledger = await _day_ledger(maker)
    assert ledger.reserved_usd == 0
    assert ledger.spent_usd == actual

    # A retried settle must not book the money twice.
    assert await budget.settle(maker, sub_correlation="c1:main", actual_usd=actual) == 0
    ledger = await _day_ledger(maker)
    assert ledger.spent_usd == actual


async def test_release_frees_reservation_without_spending(db_engine) -> None:
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker)
    await budget.reserve(
        maker,
        sub_correlation="c2:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    assert await budget.release(maker, sub_correlation="c2:main") == 4
    ledger = await _day_ledger(maker)
    assert ledger.reserved_usd == 0
    assert ledger.spent_usd == 0


async def test_retried_reserve_does_not_double_count(db_engine) -> None:
    """A crash between reserve and the model call leads to a retry with
    the SAME sub-correlation: the money must be counted exactly once."""
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker)
    estimate = Decimal("0.010000")
    for _ in range(2):
        outcome = await budget.reserve(
            maker,
            sub_correlation="c3:main",
            estimate_usd=estimate,
            chat_id=1,
            phone="+77010000001",
        )
        assert outcome.allowed
    ledger = await _day_ledger(maker)
    assert ledger.reserved_usd == estimate  # once, not twice


async def test_sweeper_releases_stale_holds(db_engine) -> None:
    """SPEC §9.2.4: a hold whose owner crashed would eat the daily limit
    until midnight — the sweeper frees it."""
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker)
    await budget.reserve(
        maker,
        sub_correlation="c4:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert await budget.sweep_stale_holds(maker, older_than=future) == 4
    ledger = await _day_ledger(maker)
    assert ledger.reserved_usd == 0
    async with maker() as db:
        statuses = (await db.execute(select(BudgetReservation.status))).scalars().all()
    assert set(statuses) == {ReservationStatus.RELEASED}


async def test_reserve_without_fx_rate_fails_closed(db_engine) -> None:
    maker = _maker(db_engine)
    with pytest.raises(PricingError):
        await budget.reserve(
            maker,
            sub_correlation="c5:main",
            estimate_usd=Decimal("0.01"),
            chat_id=1,
            phone="+77010000001",
        )


async def test_blocked_scope_releases_already_taken_scopes(db_engine) -> None:
    """The dialog limit refuses -> the day/month money taken a moment
    earlier must come back (rollback), not leak as a phantom reserve."""
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker, dialog=1)  # 1 KZT = $0.002 — too small
    outcome = await budget.reserve(
        maker,
        sub_correlation="c6:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    assert not outcome.allowed
    assert outcome.blocked_scope == BudgetScope.CHAT.value
    # The rollback undoes the whole attempt — including lazily created
    # ledger rows, so either no day row exists or it holds no reserve.
    async with _maker(db_engine)() as db:
        ledger = (
            await db.execute(
                select(BudgetLedger).where(BudgetLedger.scope == BudgetScope.GLOBAL_DAY)
            )
        ).scalar_one_or_none()
    assert ledger is None or ledger.reserved_usd == 0


async def test_threshold_alerts_once_per_period(db_engine) -> None:
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker, day=5)  # $0.01 limit; one call spends 100%
    await budget.reserve(
        maker,
        sub_correlation="c7:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    await budget.settle(maker, sub_correlation="c7:main", actual_usd=Decimal("0.010000"))
    async with maker() as db:
        alerts = (
            (
                await db.execute(
                    select(AlertNotification).where(AlertNotification.kind == "budget_day")
                )
            )
            .scalars()
            .all()
        )
    assert {a.event_key for a in alerts} == {"50pct", "80pct", "90pct", "100pct"}

    # Another settle in the same period must not create duplicates.
    await budget.reserve(
        maker,
        sub_correlation="c8:main",
        estimate_usd=Decimal("0.000001"),
        chat_id=2,
        phone="+77010000002",
    )
    # blocked (limit spent) — so seed nothing; call settle directly: no held rows -> no-op
    await budget.settle(maker, sub_correlation="c8:main", actual_usd=Decimal("0"))
    async with maker() as db:
        count = len(
            (
                await db.execute(
                    select(AlertNotification).where(AlertNotification.kind == "budget_day")
                )
            )
            .scalars()
            .all()
        )
    assert count == 4


async def test_update_open_limits_applies_today(db_engine) -> None:
    """Design review §6: an edited limit must apply to the already-open
    period, not silently start tomorrow."""
    maker = _maker(db_engine)
    await _seed_rate(maker)
    await _set_limits_kzt(maker, day=20)
    await budget.reserve(
        maker,
        sub_correlation="c9:main",
        estimate_usd=Decimal("0.010000"),
        chat_id=1,
        phone="+77010000001",
    )
    async with maker() as db, db.begin():
        changed = await budget.update_open_limits(
            db, BudgetScope.GLOBAL_DAY, Decimal("0.5"), datetime.now(UTC).date()
        )
    assert changed == 1
    ledger = await _day_ledger(maker)
    assert ledger.limit_usd == Decimal("0.5")


# ------------------------------ pricing ------------------------------


def test_cost_formula_matches_manual_math() -> None:
    """SPEC §9.1: cached input at its own rate, reasoning billed as output."""
    prices = PriceSheet(
        model="m",
        input_usd=Decimal("0.20"),
        cached_input_usd=Decimal("0.02"),
        output_usd=Decimal("1.20"),
    )
    usage = Usage(
        input_tokens=10_000, cached_input_tokens=4_000, output_tokens=300, reasoning_tokens=200
    )
    # (6000*0.20 + 4000*0.02 + 500*1.20) / 1M
    expected = (
        Decimal(6_000) * Decimal("0.20")
        + Decimal(4_000) * Decimal("0.02")
        + Decimal(500) * Decimal("1.20")
    ) / Decimal(1_000_000)
    assert compute_cost_usd(prices, usage) == expected.quantize(Decimal("0.00000001"))
    assert usd_to_kzt(Decimal("0.001"), RATE) == Decimal("0.5000")
    estimate = estimate_max_cost_usd(prices, 12_000, 350)
    assert estimate == (
        (Decimal(12_000) * Decimal("0.20") + Decimal(350) * Decimal("1.20"))
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))


async def test_price_journal_uses_effective_from(db_engine, settings) -> None:
    """A price/rate change never rewrites history: lookups at an earlier
    moment still see the earlier value."""
    maker = _maker(db_engine)
    t1 = datetime.now(UTC)
    from app.models import FxRate

    async with maker() as db, db.begin():
        db.add(FxRate(pair="USD/KZT", rate=Decimal("500"), effective_from=t1))
        db.add(
            FxRate(
                pair="USD/KZT", rate=Decimal("600"), effective_from=t1 + timedelta(hours=1)
            )
        )
        for unit, price in (
            (PriceUnit.PER_1M_INPUT, Decimal("0.20")),
            (PriceUnit.PER_1M_CACHED_INPUT, Decimal("0.02")),
            (PriceUnit.PER_1M_OUTPUT, Decimal("1.20")),
        ):
            db.add(ModelPrice(model="m", unit=unit, price_usd=price, effective_from=t1))
        db.add(
            ModelPrice(
                model="m",
                unit=PriceUnit.PER_1M_INPUT,
                price_usd=Decimal("0.40"),
                effective_from=t1 + timedelta(hours=1),
            )
        )
    async with maker() as db:
        assert await load_usd_kzt_rate(db, at=t1) == Decimal("500")
        assert await load_usd_kzt_rate(db, at=t1 + timedelta(hours=2)) == Decimal("600")
        sheet_before = await load_price_sheet(db, "m", at=t1)
        sheet_after = await load_price_sheet(db, "m", at=t1 + timedelta(hours=2))
    assert sheet_before.input_usd == Decimal("0.20")
    assert sheet_after.input_usd == Decimal("0.40")


async def test_missing_price_fails_closed_and_seed_is_idempotent(
    db_engine, settings
) -> None:
    maker = _maker(db_engine)
    async with maker() as db:
        with pytest.raises(PricingError):
            await load_price_sheet(db, settings.openai_chat_model)
    async with maker() as db, db.begin():
        created_first = await seed_default_prices(db, settings)
    async with maker() as db, db.begin():
        created_again = await seed_default_prices(db, settings)
    assert len(created_first) == 5
    assert created_again == []
    async with maker() as db:
        sheet = await load_price_sheet(db, settings.openai_chat_model)
    assert sheet.input_usd == Decimal("0.20")
    assert sheet.output_usd == Decimal("1.20")
