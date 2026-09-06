"""Atomic budget reservation, settle/release and stale-hold sweeping.

The decision "is there money" is made by PostgreSQL in the same UPDATE
that takes the money (SPEC §9.2) — a Python-side check is NOT a limit.

Concurrency rules (from the phase 6 design review):
- scopes are always processed in one canonical order (scope, scope_key),
  so two parallel reservations cannot deadlock on ledger rows;
- reserve runs in its OWN short transaction: a partial failure is undone
  by ROLLBACK and never touches the caller's transaction;
- reservations are idempotent per (sub_correlation, ledger): a retried
  attempt that finds its reservation already held does NOT increment the
  ledger again;
- limits are entered in ₸ and converted to USD with the owner-entered
  rate when a ledger period row is first created. Missing prices/rate
  fail CLOSED (PricingError) — no rate, no spending.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import AlertNotification, BudgetLedger, BudgetReservation, SystemEvent
from app.models.enums import BudgetScope, ReservationStatus
from app.services import app_settings
from app.services.app_settings import get_setting
from app.services.pricing import kzt_to_usd, load_usd_kzt_rate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

# The chat scope is a per-dialog lifetime budget: its period must be the
# same constant whenever the row is (lazily) created, or the UNIQUE key
# would mint a fresh budget per creation day.
_CHAT_PERIOD_START = date(1970, 1, 1)
_CHAT_PERIOD_END = date(9999, 12, 31)

# Alert thresholds, % of the period limit (SPEC §9.3).
_THRESHOLDS = (50, 80, 90, 100)

_SCOPE_LIMIT_KEYS = {
    BudgetScope.GLOBAL_DAY: app_settings.DAILY_AI_BUDGET_KZT,
    BudgetScope.GLOBAL_MONTH: app_settings.MONTHLY_AI_BUDGET_KZT,
    BudgetScope.CHAT: app_settings.MAX_AI_COST_PER_DIALOG_KZT,
    BudgetScope.USER_DAY: app_settings.MAX_AI_COST_PER_USER_PER_DAY_KZT,
}


@dataclass(frozen=True)
class ScopeSpec:
    scope: BudgetScope
    scope_key: str
    period_start: date
    period_end: date


@dataclass(frozen=True)
class ReserveOutcome:
    allowed: bool
    blocked_scope: str | None = None


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12  # noqa: PLR2004
        else start.replace(month=start.month + 1)
    )
    return start, end


def build_scopes(chat_id: int | None, phone: str | None, now: datetime) -> list[ScopeSpec]:
    """All applicable scopes, in the canonical (deadlock-free) order.

    Period boundaries are UTC dates: predictable and testable; the admin
    UI states this explicitly.
    """
    day = now.astimezone(UTC).date()
    month_start, next_month = _month_bounds(day)
    specs = [
        ScopeSpec(BudgetScope.GLOBAL_DAY, day.isoformat(), day, day),
        ScopeSpec(BudgetScope.GLOBAL_MONTH, day.strftime("%Y-%m"), month_start, next_month),
    ]
    if chat_id is not None:
        specs.append(
            ScopeSpec(BudgetScope.CHAT, f"chat:{chat_id}", _CHAT_PERIOD_START, _CHAT_PERIOD_END)
        )
    if phone:
        specs.append(ScopeSpec(BudgetScope.USER_DAY, f"user:{phone}:{day.isoformat()}", day, day))
    specs.sort(key=lambda s: (s.scope.value, s.scope_key))
    return specs


async def _ensure_ledgers(
    db: AsyncSession, specs: list[ScopeSpec], rate: Decimal
) -> dict[ScopeSpec, int]:
    """Lazily create the period counter rows (canonical order) and return
    their ids. limit_usd is fixed from settings at creation; later limit
    edits update open periods via update_open_limits()."""
    for spec in specs:
        limit_kzt = Decimal(str(await get_setting(db, _SCOPE_LIMIT_KEYS[spec.scope])))
        await db.execute(
            pg_insert(BudgetLedger)
            .values(
                scope=spec.scope,
                scope_key=spec.scope_key,
                period_start=spec.period_start,
                period_end=spec.period_end,
                limit_usd=kzt_to_usd(limit_kzt, rate),
            )
            .on_conflict_do_nothing(constraint="uq_budget_ledger_scope")
        )
    ids: dict[ScopeSpec, int] = {}
    for spec in specs:
        ids[spec] = (
            await db.execute(
                select(BudgetLedger.id).where(
                    BudgetLedger.scope == spec.scope,
                    BudgetLedger.scope_key == spec.scope_key,
                    BudgetLedger.period_start == spec.period_start,
                )
            )
        ).scalar_one()
    return ids


async def reserve(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    sub_correlation: str,
    estimate_usd: Decimal,
    chat_id: int | None,
    phone: str | None,
    now: datetime | None = None,
) -> ReserveOutcome:
    """Reserve estimate_usd across every applicable scope, atomically.

    sub_correlation is "{correlation}:main" / ":summary" etc — one paid
    call, one reservation set. Any scope refusing = ROLLBACK of the whole
    attempt and ReserveOutcome(allowed=False, blocked_scope=...).
    """
    moment = now or datetime.now(UTC)
    specs = build_scopes(chat_id, phone, moment)
    async with sessionmaker() as db:
        rate = await load_usd_kzt_rate(db, moment)  # PricingError -> fail closed
        ledger_ids = await _ensure_ledgers(db, specs, rate)
        for spec in specs:
            ledger_id = ledger_ids[spec]
            inserted = (
                await db.execute(
                    pg_insert(BudgetReservation)
                    .values(
                        ledger_id=ledger_id,
                        correlation_id=sub_correlation,
                        amount_usd=estimate_usd,
                        status=ReservationStatus.HELD,
                    )
                    .on_conflict_do_nothing(constraint="uq_budget_reservations_correlation")
                    .returning(BudgetReservation.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                # A previous attempt already holds (or settled) this money
                # for this exact call — counting it again would double-spend
                # the estimate until the sweeper noticed.
                continue
            took = (
                await db.execute(
                    update(BudgetLedger)
                    .where(
                        BudgetLedger.id == ledger_id,
                        BudgetLedger.reserved_usd + BudgetLedger.spent_usd + estimate_usd
                        <= BudgetLedger.limit_usd,
                    )
                    .values(reserved_usd=BudgetLedger.reserved_usd + estimate_usd)
                    .returning(BudgetLedger.id)
                )
            ).scalar_one_or_none()
            if took is None:
                await db.rollback()  # releases every scope taken above
                logger.info(
                    "budget_blocked",
                    scope=spec.scope.value,
                    scope_key=spec.scope_key,
                    correlation_id=sub_correlation,
                )
                return ReserveOutcome(allowed=False, blocked_scope=spec.scope.value)
        await db.commit()
    return ReserveOutcome(allowed=True)


async def _close_holds(
    db: AsyncSession,
    *,
    sub_correlation: str,
    actual_usd: Decimal,
    final_status: ReservationStatus,
) -> int:
    """Move held reservations to a terminal status; idempotent because a
    retried call finds no 'held' rows. Locks follow the canonical order."""
    rows = (
        await db.execute(
            select(BudgetReservation, BudgetLedger)
            .join(BudgetLedger, BudgetLedger.id == BudgetReservation.ledger_id)
            .where(
                BudgetReservation.correlation_id == sub_correlation,
                BudgetReservation.status == ReservationStatus.HELD,
            )
            .order_by(BudgetLedger.scope, BudgetLedger.scope_key)
            .with_for_update()
        )
    ).all()
    now = datetime.now(UTC)
    for reservation, ledger in rows:
        reservation.status = final_status
        reservation.settled_at = now
        # Never drive reserved below zero even if numbers drifted.
        ledger.reserved_usd = max(ledger.reserved_usd - reservation.amount_usd, Decimal(0))
        if final_status is ReservationStatus.SETTLED:
            ledger.spent_usd = ledger.spent_usd + actual_usd
    return len(rows)


async def settle(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    sub_correlation: str,
    actual_usd: Decimal,
) -> int:
    """Book the FACTUAL cost and free the remainder of the estimate."""
    async with sessionmaker() as db:
        settled = await _close_holds(
            db,
            sub_correlation=sub_correlation,
            actual_usd=actual_usd,
            final_status=ReservationStatus.SETTLED,
        )
        if settled:
            await _record_threshold_alerts(db)
        await db.commit()
    return settled


async def release(sessionmaker: async_sessionmaker[AsyncSession], *, sub_correlation: str) -> int:
    """Free a reservation after a failed call — nothing was spent."""
    async with sessionmaker() as db:
        released = await _close_holds(
            db,
            sub_correlation=sub_correlation,
            actual_usd=Decimal(0),
            final_status=ReservationStatus.RELEASED,
        )
        await db.commit()
    return released


async def sweep_stale_holds(
    sessionmaker: async_sessionmaker[AsyncSession], *, older_than: datetime
) -> int:
    """Free holds whose owner crashed before settle/release: without this
    a dead reservation eats the daily limit until midnight (SPEC §9.2.4)."""
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(BudgetReservation, BudgetLedger)
                .join(BudgetLedger, BudgetLedger.id == BudgetReservation.ledger_id)
                .where(
                    BudgetReservation.status == ReservationStatus.HELD,
                    BudgetReservation.created_at < older_than,
                )
                .order_by(BudgetLedger.scope, BudgetLedger.scope_key)
                .with_for_update(skip_locked=True)
            )
        ).all()
        now = datetime.now(UTC)
        for reservation, ledger in rows:
            reservation.status = ReservationStatus.RELEASED
            reservation.settled_at = now
            ledger.reserved_usd = max(ledger.reserved_usd - reservation.amount_usd, Decimal(0))
        if rows:
            db.add(
                SystemEvent(
                    level="warning",
                    component="budget",
                    event="stale_holds_released",
                    detail={"count": len(rows)},
                )
            )
            logger.warning("budget_stale_holds_released", count=len(rows))
        await db.commit()
    return len(rows)


async def update_open_limits(
    db: AsyncSession, scope: BudgetScope, new_limit_usd: Decimal, today: date
) -> int:
    """Apply an edited limit to already-open ledger periods — otherwise a
    changed limit would only take effect tomorrow (design review §6)."""
    result = await db.execute(
        update(BudgetLedger)
        .where(BudgetLedger.scope == scope, BudgetLedger.period_end >= today)
        .values(limit_usd=new_limit_usd)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _record_threshold_alerts(db: AsyncSession) -> None:
    """50/80/90/100% of the day/month budget — once per threshold per
    period (UNIQUE dedup); WhatsApp delivery itself lands in phase 9."""
    today = datetime.now(UTC).date()
    kinds = {
        BudgetScope.GLOBAL_DAY: ("budget_day", today.isoformat()),
        BudgetScope.GLOBAL_MONTH: ("budget_month", today.strftime("%Y-%m")),
    }
    for scope, (kind, period_key) in kinds.items():
        ledger = (
            await db.execute(
                select(BudgetLedger).where(
                    BudgetLedger.scope == scope, BudgetLedger.scope_key == period_key
                )
            )
        ).scalar_one_or_none()
        if ledger is None or ledger.limit_usd <= 0:
            continue
        pct = int(ledger.spent_usd / ledger.limit_usd * 100)
        for threshold in _THRESHOLDS:
            if pct < threshold:
                break
            inserted = (
                await db.execute(
                    pg_insert(AlertNotification)
                    .values(
                        kind=kind,
                        event_key=f"{threshold}pct",
                        period_key=period_key,
                        detail={
                            "spent_usd": str(ledger.spent_usd),
                            "limit_usd": str(ledger.limit_usd),
                        },
                    )
                    .on_conflict_do_nothing(constraint="uq_alert_notifications_dedupe")
                    .returning(AlertNotification.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                db.add(
                    SystemEvent(
                        level="warning" if threshold < 100 else "critical",  # noqa: PLR2004
                        component="budget",
                        event=f"{kind}_{threshold}pct",
                        detail={"period": period_key, "spent_usd": str(ledger.spent_usd)},
                    )
                )
