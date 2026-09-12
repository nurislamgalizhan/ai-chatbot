"""Reports: Обзор, Расходы, Аналитика, and their export.

Reading a report NEVER calls OpenAI (SPEC invariant 11/16) — everything
here is a few indexed aggregate queries over rows that already exist.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analytics import (
    AnalyticsResponse,
    CostsResponse,
    DashboardResponse,
    DaySpend,
    DayTokens,
    PeriodInfo,
)
from app.security.deps import get_app_settings, get_db, require_admin
from app.services import analytics, app_settings
from app.services.app_settings import get_setting

router = APIRouter(
    prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(require_admin)]
)

DaysQuery = Annotated[int, Query(ge=1, le=analytics.MAX_DAYS)]

# Metrics SPEC §12.5/§12.7 lists that the current schema cannot support
# truthfully. Sent to the UI so the owner sees WHY a number is absent
# instead of a made-up chart (each needs a schema change to appear).
NOT_MEASURED = [
    "Среднее время ответа клиенту: в режиме приёмки (DRY_RUN) сообщения не "
    "отправляются, поэтому времени отправки не существует. Появится после "
    "включения реальной отправки.",
    "Латентность по этапам (приём → очередь → поиск по базе → модель → отправка): "
    "записывается только время вызова модели; отметок для остальных шагов в базе нет.",
    "«Сколько денег сэкономил кэш и FAQ»: число бесплатных ответов считается точно, "
    "а вот сколько стоил бы каждый из них — только предположение, поэтому суммы нет.",
    "Ответ оператора нельзя связать с конкретным вопросом клиента: у сообщений с "
    "телефона такой связи не существует.",
    "Курс RUB/KZT: ни один расход системы не в рублях (OpenAI — в долларах).",
]


def _period(days: int) -> analytics.Period:
    return analytics.period_from_days(days)


def _period_info(period: analytics.Period) -> PeriodInfo:
    return PeriodInfo(start=period.start, end=period.end, days=period.days)


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> DashboardResponse:
    settings = get_app_settings(request)
    data = await analytics.dashboard(db)
    emergency_off = bool(await get_setting(db, app_settings.EMERGENCY_AI_OFF))
    return DashboardResponse(
        **data,
        dry_run=settings.dry_run,
        ai_enabled=not emergency_off,
    )


@router.get("/costs", response_model=CostsResponse)
async def costs(
    db: Annotated[AsyncSession, Depends(get_db)], days: DaysQuery = 30
) -> CostsResponse:
    period = _period(days)
    spend = await analytics.spend_by_day(db, period)
    tokens = await analytics.tokens_by_day(db, period)
    return CostsResponse(
        period=_period_info(period),
        labels=analytics.call_type_labels(),
        spend_by_day=[
            DaySpend(
                day=row.day,
                by_call_type={k: str(v) for k, v in row.by_call_type.items()},
                total_usd=str(row.total_usd),
                total_kzt=str(row.total_kzt),
            )
            for row in spend
        ],
        tokens_by_day=[
            DayTokens(
                day=row.day,
                input_tokens=row.input_tokens,
                cached_input_tokens=row.cached_input_tokens,
                output_tokens=row.output_tokens,
                reasoning_tokens=row.reasoning_tokens,
                audio_seconds=row.audio_seconds,
                cost_usd=str(row.cost_usd),
                cost_kzt=str(row.cost_kzt),
            )
            for row in tokens
        ],
        by_model=await analytics.spend_by_model(db, period),
        top_dialogs=await analytics.top_dialogs(db, period),
        budget=await analytics.budget_state(db),
        total_usd=str(sum((row.total_usd for row in spend), start=spend[0].total_usd * 0)),
        total_kzt=str(sum((row.total_kzt for row in spend), start=spend[0].total_kzt * 0)),
    )


@router.get("", response_model=AnalyticsResponse)
async def overview(
    db: Annotated[AsyncSession, Depends(get_db)], days: DaysQuery = 30
) -> AnalyticsResponse:
    period = _period(days)
    return AnalyticsResponse(
        period=_period_info(period),
        workload=await analytics.workload(db, period),
        dialogs=await analytics.dialog_stats(db, period),
        answer_sources=await analytics.answer_sources(db, period),
        errors=await analytics.errors(db, period),
        language_gaps=await analytics.language_gaps(db, period),
        semantic_cache_enabled=bool(await get_setting(db, app_settings.SEMANTIC_CACHE_ENABLED)),
        not_measured=NOT_MEASURED,
    )


# --------------------------------- export ---------------------------------

_EXPORT_HEADERS = [
    "Дата",
    "Входящие токены",
    "Из них из кэша",
    "Исходящие токены",
    "Токены размышления",
    "Секунды аудио",
    "Расход, $",
    "Расход, ₸",
]


def _export_rows(tokens: list[analytics.DailyTokens]) -> list[list[object]]:
    return [
        [
            row.day.isoformat(),
            row.input_tokens,
            row.cached_input_tokens,
            row.output_tokens,
            row.reasoning_tokens,
            row.audio_seconds,
            float(row.cost_usd),
            float(row.cost_kzt),
        ]
        for row in tokens
    ]


@router.get("/costs/export")
async def export_costs(
    db: Annotated[AsyncSession, Depends(get_db)],
    days: DaysQuery = 30,
    fmt: Annotated[str, Query(pattern="^(csv|xlsx)$")] = "xlsx",
) -> Response:
    """The same table the page shows, as a file.

    CSV is written with a BOM and semicolons so Excel in a Russian locale
    opens it correctly instead of putting every row in one column.
    """
    period = _period(days)
    rows = _export_rows(await analytics.tokens_by_day(db, period))
    stamp = f"{period.start.isoformat()}_{period.end.isoformat()}"

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(_EXPORT_HEADERS)
        writer.writerows(rows)
        payload = buffer.getvalue().encode("utf-8-sig")
        media_type = "text/csv; charset=utf-8"
        name = f"raskhody_{stamp}.csv"
    else:
        from openpyxl import Workbook  # noqa: PLC0415 - only this endpoint needs it

        book = Workbook()
        sheet = book.active
        sheet.title = "Расходы"
        sheet.append(_EXPORT_HEADERS)
        for row in rows:
            sheet.append(row)
        for index, header in enumerate(_EXPORT_HEADERS, start=1):
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = max(
                len(header) + 2, 12
            )
        stream = io.BytesIO()
        book.save(stream)
        payload = stream.getvalue()
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        name = f"raskhody_{stamp}.xlsx"

    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Generated-At": datetime.now(UTC).isoformat(),
        },
    )
