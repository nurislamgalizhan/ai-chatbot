"""«Система»: what state everything is in, without touching OpenAI.

Opening this page must be free (SPEC invariant 11/16), so nothing here
calls a paid provider. The one outbound call is «Проверить подключение»,
and it goes to GREEN-API's free `getStateInstance` — only when the owner
presses the button.

Secrets are never returned, not even partially, beyond the last four
characters (SPEC §12.9).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertNotification, SystemEvent, WhatsappInstance
from app.schemas.system_status import (
    AlertRow,
    AlertsStatus,
    BackupStatus,
    ConnectionCheckResponse,
    DiskStatus,
    QueueStatus,
    SystemEventRow,
    SystemEventsResponse,
    SystemStatusResponse,
    TestAlertResponse,
    TransportStatus,
)
from app.security.deps import get_app_settings, get_db, require_admin
from app.security.secrets import mask_secret
from app.services import app_settings, monitoring
from app.services.alerts import alert_text, hour_bucket, record_alert
from app.services.app_settings import get_setting
from app.whatsapp.green_api import GreenApiProvider, settings_problems
from app.whatsapp.provider import InstanceHealth, ProviderError

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_admin)])

_EVENT_LIMIT_DEFAULT = 50


@router.get("/status", response_model=SystemStatusResponse)
async def status(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> SystemStatusResponse:
    settings = get_app_settings(request)
    snapshot = await monitoring.collect(db, media_dir=settings.media_dir)

    instance = await _active_instance(db)
    db_size = (
        await db.execute(select(func.pg_database_size(func.current_database())))
    ).scalar_one()

    alert_phone = str(await get_setting(db, app_settings.ALERT_PHONE) or "")
    alerts_enabled = bool(await get_setting(db, app_settings.ALERTS_ENABLED))
    pending_alerts = (
        await db.execute(
            select(func.count())
            .select_from(AlertNotification)
            .where(AlertNotification.status.in_(("pending", "queued")))
        )
    ).scalar_one()
    failed_alerts = (
        await db.execute(
            select(func.count())
            .select_from(AlertNotification)
            .where(
                AlertNotification.status == "failed",
                # A window, not a lifetime: retention never deletes
                # failed rows, so an outage a year ago would otherwise
                # be reported forever.
                AlertNotification.created_at >= datetime.now(UTC) - timedelta(days=7),
            )
        )
    ).scalar_one()
    last_alerts = (
        (await db.execute(select(AlertNotification).order_by(AlertNotification.id.desc()).limit(5)))
        .scalars()
        .all()
    )

    return SystemStatusResponse(
        dry_run=settings.dry_run,
        emergency_ai_off=bool(await get_setting(db, app_settings.EMERGENCY_AI_OFF)),
        transport=TransportStatus(
            configured=bool(settings.green_api_id_instance),
            instance_id=instance.instance_id if instance else settings.green_api_id_instance or "",
            token_hint=mask_secret(settings.green_api_token),
            last_polled_at=instance.last_polled_at if instance else None,
            last_error=instance.last_error if instance else None,
            state=instance.state if instance else None,
            state_checked_at=instance.state_checked_at if instance else None,
            settings_checked_at=instance.settings_checked_at if instance else None,
            settings_problems=settings_problems(
                dict(instance.settings_json or {}) if instance else {}
            ),
            provider_settings=dict(instance.settings_json or {}) if instance else {},
        ),
        model=settings.openai_chat_model,
        openai_configured=bool(settings.openai_api_key),
        queues=QueueStatus(
            queue_depth=snapshot.queue_depth,
            oldest_queued_minutes=snapshot.oldest_queued_minutes,
            inbox_backlog=snapshot.inbox_backlog,
            outbox_pending=snapshot.outbox_pending,
            failed_24h=snapshot.failed_24h,
        ),
        disk=DiskStatus(
            used_percent=snapshot.disk_used_percent,
            free_gb=round(snapshot.disk_free_bytes / 1024**3, 1),
            total_gb=round(snapshot.disk_total_bytes / 1024**3, 1),
            database_mb=round(int(db_size) / 1024**2, 1),
        ),
        backup=BackupStatus(
            last_at=snapshot.last_backup_at,
            age_hours=snapshot.backup_age_hours,
            stale_after_hours=int(await get_setting(db, app_settings.BACKUP_STALE_HOURS)),
        ),
        alerts=AlertsStatus(
            enabled=alerts_enabled,
            phone_hint=mask_secret(alert_phone, keep=4) if alert_phone else "",
            pending=int(pending_alerts),
            failed=int(failed_alerts),
            recent=[
                AlertRow(
                    id=row.id,
                    kind=row.kind,
                    event_key=row.event_key,
                    # Rows written before phase 9 have no stored text
                    # (the column did not exist): compose it now rather
                    # than showing the owner an empty line.
                    body=row.body or alert_text(row.kind, row.event_key, row.detail),
                    status=row.status,
                    created_at=row.created_at,
                    sent_at=row.sent_at,
                    last_error=row.last_error,
                )
                for row in last_alerts
            ],
        ),
    )


@router.get("/events", response_model=SystemEventsResponse)
async def events(
    db: Annotated[AsyncSession, Depends(get_db)],
    level: Annotated[str, Query(pattern="^(all|error)$")] = "all",
    limit: Annotated[int, Query(ge=1, le=200)] = _EVENT_LIMIT_DEFAULT,
) -> SystemEventsResponse:
    """Technical history. Never contains customer text: every writer puts
    ids and codes into `detail`, never message bodies."""
    query = select(SystemEvent).order_by(SystemEvent.id.desc()).limit(limit)
    if level == "error":
        query = query.where(SystemEvent.level.in_(("error", "critical")))
    rows = (await db.execute(query)).scalars().all()
    return SystemEventsResponse(
        items=[
            SystemEventRow(
                id=row.id,
                level=row.level,
                component=row.component,
                event=row.event,
                detail=_safe_detail(row.detail),
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Second line of defence for the status page.

    Writers are supposed to keep customer text out of `detail`; a page
    that renders whatever is in the database should not depend on every
    one of them having got it right forever.
    """
    if not detail:
        return {}
    safe: dict[str, Any] = {}
    for key, value in detail.items():
        if key in {"text", "body", "message", "caption", "transcript", "question"}:
            continue
        safe[key] = value if not isinstance(value, str) else value[:200]
    return safe


@router.post("/check-connection", response_model=ConnectionCheckResponse)
async def check_connection(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> ConnectionCheckResponse:
    """Ask GREEN-API for the instance state. Free call, owner-initiated.

    OpenAI is deliberately NOT probed: every call to it costs money, and
    a status page must never spend any (SPEC invariant 11).
    """
    settings = get_app_settings(request)
    if not settings.green_api_id_instance or not settings.green_api_token:
        return ConnectionCheckResponse(ok=False, state=None, error="not_configured")
    provider = GreenApiProvider(settings)
    try:
        state = await provider.get_instance_state()
        # Authorization is only half the answer: the instance can be
        # authorized while its settings send every notification to a
        # webhook we do not run — and then the bot answers nobody.
        health = await provider.get_health()
    except ProviderError as exc:
        logger.warning("instance_state_check_failed", status_code=exc.status_code)
        await _store_state(db, state=None, error=f"provider:{exc.status_code}")
        return ConnectionCheckResponse(ok=False, state=None, error="unreachable")
    finally:
        await provider.aclose()
    await _store_state(db, state=state, error=None, health=health)
    return ConnectionCheckResponse(
        ok=state == "authorized" and not health.settings_problems,
        state=state,
        error=None,
        problems=health.settings_problems,
    )


async def _active_instance(db: AsyncSession) -> WhatsappInstance | None:
    """The instance actually in use. Ordering by id alone would show the
    state of a retired instance after the owner switches numbers."""
    return (
        await db.execute(
            select(WhatsappInstance)
            .where(WhatsappInstance.is_active.is_(True))
            .order_by(WhatsappInstance.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _store_state(
    db: AsyncSession,
    *,
    state: str | None,
    error: str | None,
    health: InstanceHealth | None = None,
) -> None:
    instance = await _active_instance(db)
    if instance is None:
        return
    instance.state_checked_at = datetime.now(UTC)
    if state is not None:
        instance.state = state
        instance.last_error = None
    if error is not None:
        instance.last_error = error
    if health is not None and health.settings:
        # Kept so «Система» can show the configuration without calling
        # the provider on every page render.
        instance.settings_json = dict(health.settings)
        instance.settings_checked_at = datetime.now(UTC)
    db.add(
        SystemEvent(
            level="info" if error is None else "warning",
            component="system",
            event="instance_state_checked",
            detail={"state": state, "error": error},
        )
    )
    await db.commit()


@router.post("/test-alert", response_model=TestAlertResponse)
async def test_alert(db: Annotated[AsyncSession, Depends(get_db)]) -> TestAlertResponse:
    """Queue a real alert so the owner can confirm alerts actually arrive.

    Queued, not sent from here: delivery is the worker's single path, and
    that is exactly the path being tested. The period key is the minute,
    so pressing twice in a row is not silently swallowed by the dedup.
    """
    phone = str(await get_setting(db, app_settings.ALERT_PHONE) or "").strip()
    enabled = bool(await get_setting(db, app_settings.ALERTS_ENABLED))
    created = await record_alert(
        db,
        kind="system",
        event_key="test",
        period_key=f"{hour_bucket()}:{datetime.now(UTC):%M}",
        detail={},
    )
    await db.commit()
    # An honest answer: a button that always says "sent" teaches the
    # owner to trust a channel that may not be configured at all.
    if not created:
        return TestAlertResponse(queued=False, will_send=False, reason="already_queued")
    if not phone:
        return TestAlertResponse(queued=True, will_send=False, reason="no_phone")
    if not enabled:
        return TestAlertResponse(queued=True, will_send=False, reason="alerts_off")
    return TestAlertResponse(queued=True, will_send=True, reason=None)
