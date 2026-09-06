"""Лимиты и безопасность расходов (SPEC §12.6).

Budget limit edits also update ALREADY-OPEN ledger periods (design
review §6) — otherwise a changed daily limit would only apply tomorrow.
Prices and the USD/KZT rate are JOURNALS: a change inserts a new row
effective now; history is never rewritten.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser, BudgetLedger, FxRate, ModelPrice
from app.models.enums import BudgetScope, PriceUnit
from app.schemas.limits import (
    FxRateRequest,
    LedgerSnapshot,
    LimitsResponse,
    LimitsSettings,
    LimitsUpdateRequest,
    OkResponse,
    PriceItem,
    PriceRequest,
)
from app.security.audit import record_audit
from app.security.deps import client_ip, get_app_settings, get_db, require_admin
from app.services import app_settings
from app.services.app_settings import get_setting, set_setting
from app.services.budget import update_open_limits
from app.services.pricing import USD_KZT, PricingError, kzt_to_usd, load_usd_kzt_rate

router = APIRouter(prefix="/api/limits", tags=["limits"], dependencies=[Depends(require_admin)])

_BUDGET_SCOPE_BY_KEY = {
    app_settings.DAILY_AI_BUDGET_KZT: BudgetScope.GLOBAL_DAY,
    app_settings.MONTHLY_AI_BUDGET_KZT: BudgetScope.GLOBAL_MONTH,
    app_settings.MAX_AI_COST_PER_DIALOG_KZT: BudgetScope.CHAT,
    app_settings.MAX_AI_COST_PER_USER_PER_DAY_KZT: BudgetScope.USER_DAY,
}


async def _current_settings(db: AsyncSession) -> LimitsSettings:
    values = {name: await get_setting(db, name) for name in LimitsSettings.model_fields}
    return LimitsSettings(**values)


@router.get("", response_model=LimitsResponse)
async def get_limits(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> LimitsResponse:
    settings = get_app_settings(request)
    prices = (
        (
            await db.execute(
                select(ModelPrice).order_by(
                    ModelPrice.model, ModelPrice.unit, ModelPrice.effective_from.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[tuple[str, PriceUnit], ModelPrice] = {}
    for price in prices:
        latest.setdefault((price.model, price.unit), price)
    try:
        rate = await load_usd_kzt_rate(db)
    except PricingError:
        rate = None
    today = datetime.now(UTC).date()
    ledgers = (
        (
            await db.execute(
                select(BudgetLedger).where(
                    BudgetLedger.scope.in_((BudgetScope.GLOBAL_DAY, BudgetScope.GLOBAL_MONTH)),
                    BudgetLedger.period_end >= today,
                )
            )
        )
        .scalars()
        .all()
    )
    return LimitsResponse(
        settings=await _current_settings(db),
        prices=[
            PriceItem(
                model=p.model,
                unit=p.unit.value,
                price_usd=p.price_usd,
                effective_from=p.effective_from,
            )
            for p in latest.values()
        ],
        usd_kzt_rate=rate,
        ledgers=[
            LedgerSnapshot(
                scope=ledger.scope.value,
                period=ledger.scope_key,
                limit_usd=ledger.limit_usd,
                spent_usd=ledger.spent_usd,
                reserved_usd=ledger.reserved_usd,
            )
            for ledger in ledgers
        ],
        dry_run=settings.dry_run,
    )


@router.put("", response_model=LimitsResponse)
async def update_limits(
    body: LimitsUpdateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LimitsResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    current = await _current_settings(db)
    changed_budget_keys: list[str] = []
    for name, new_value in body.settings.model_dump().items():
        if getattr(current, name) == new_value:
            continue
        await set_setting(db, name, new_value, actor_user_id=user.id, correlation_id=correlation_id)
        if name in _BUDGET_SCOPE_BY_KEY:
            changed_budget_keys.append(name)

    if changed_budget_keys:
        try:
            rate = await load_usd_kzt_rate(db)
        except PricingError:
            rate = None  # no open periods can exist without a rate anyway
        if rate is not None:
            today = datetime.now(UTC).date()
            for name in changed_budget_keys:
                new_limit_usd = kzt_to_usd(Decimal(str(getattr(body.settings, name))), rate)
                await update_open_limits(db, _BUDGET_SCOPE_BY_KEY[name], new_limit_usd, today)
    return await get_limits(request, db)


@router.post("/fx", response_model=OkResponse)
async def set_fx_rate(
    body: FxRateRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkResponse:
    """Journal insert: history stays, historical cost_kzt never changes."""
    db.add(
        FxRate(
            pair=USD_KZT,
            rate=body.rate,
            effective_from=datetime.now(UTC),
            changed_by=user.id,
        )
    )
    await record_audit(
        db,
        action="limits.fx_rate_set",
        actor_user_id=user.id,
        entity_type="fx_rate",
        entity_id=USD_KZT,
        after={"rate": str(body.rate)},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return OkResponse(detail={"rate": str(body.rate)})


@router.post("/prices", response_model=OkResponse)
async def set_price(
    body: PriceRequest,
    request: Request,
    user: Annotated[AdminUser, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkResponse:
    db.add(
        ModelPrice(
            model=body.model,
            unit=PriceUnit(body.unit),
            price_usd=body.price_usd,
            effective_from=datetime.now(UTC),
            created_by=user.id,
        )
    )
    await record_audit(
        db,
        action="limits.price_set",
        actor_user_id=user.id,
        entity_type="model_price",
        entity_id=f"{body.model}:{body.unit}",
        after={"price_usd": str(body.price_usd)},
        ip=client_ip(request),
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return OkResponse(detail={"model": body.model, "unit": body.unit})
