"""What the owner needs to know without opening the panel.

The AI pipeline already reports its own troubles (budget, breaker,
polling). This module watches the things nobody reports because they are
nobody's step: free disk, a queue that stops draining, a database dump
that silently stopped running.

Everything here is cheap: three indexed counts and a `statvfs`. It runs
on a slow schedule in the worker and writes to the same two places
everything else does — `system_events` for the «Система» page and
`alert_notifications` for WhatsApp.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from app.models import InboundNotification, Message, SystemEvent, WaOutbox, WhatsappInstance
from app.models.enums import MsgState
from app.services import app_settings
from app.services.alerts import hour_bucket, record_alert
from app.services.app_settings import get_setting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.whatsapp.provider import WhatsAppProvider

logger = structlog.get_logger(__name__)

#: Written by deploy/backup/pg_backup.sh after a successful dump. The
#: backend never reads the backup directory itself: it runs read-only and
#: has no business seeing the host filesystem.
BACKUP_EVENT = "backup_completed"


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """One reading of everything the status page shows."""

    queue_depth: int
    oldest_queued_minutes: int
    inbox_backlog: int
    outbox_pending: int
    failed_24h: int
    disk_total_bytes: int
    disk_free_bytes: int
    last_backup_at: datetime | None

    @property
    def disk_used_percent(self) -> int:
        if self.disk_total_bytes <= 0:
            return 0
        used = self.disk_total_bytes - self.disk_free_bytes
        return int(used / self.disk_total_bytes * 100)

    @property
    def backup_age_hours(self) -> int | None:
        if self.last_backup_at is None:
            return None
        delta = datetime.now(UTC) - self.last_backup_at
        return int(delta.total_seconds() // 3600)


def disk_usage_for(path: str) -> shutil._ntuple_diskusage:
    """Disk usage of the filesystem behind `path`.

    The media volume may not be mounted where this process runs (the API
    container mounts it read-only; a host-side tool may have no volume at
    all). Reporting the nearest existing parent is still a true number
    for the same disk — a crash here would make the whole «Система» page
    unavailable over a directory that is only relevant to the worker.
    """
    candidate = Path(path)
    for probe in (candidate, *candidate.parents):
        if probe.exists():
            if probe != candidate:
                logger.warning("media_dir_missing_using_parent", media_dir=path, used=str(probe))
            return shutil.disk_usage(probe)
    return shutil.disk_usage(Path.cwd())  # pragma: no cover - "/" always exists


async def collect(db: AsyncSession, *, media_dir: str) -> SystemSnapshot:
    now = datetime.now(UTC)
    queue_depth = (
        await db.execute(
            select(func.count()).select_from(Message).where(Message.state == MsgState.QUEUED)
        )
    ).scalar_one()
    oldest = (
        await db.execute(
            select(func.min(Message.created_at)).where(Message.state == MsgState.QUEUED)
        )
    ).scalar()
    inbox_backlog = (
        await db.execute(
            select(func.count())
            .select_from(InboundNotification)
            .where(InboundNotification.status == "stored")
        )
    ).scalar_one()
    outbox_pending = (
        await db.execute(
            select(func.count())
            .select_from(WaOutbox)
            .where(WaOutbox.state.in_(("PENDING", "FAILED_RETRYABLE")))
        )
    ).scalar_one()
    failed_24h = (
        await db.execute(
            select(func.count())
            .select_from(SystemEvent)
            .where(
                SystemEvent.level.in_(("error", "critical")),
                SystemEvent.created_at >= now - timedelta(hours=24),
            )
        )
    ).scalar_one()
    last_backup = (
        await db.execute(
            select(func.max(SystemEvent.created_at)).where(
                SystemEvent.component == "backup", SystemEvent.event == BACKUP_EVENT
            )
        )
    ).scalar()

    usage = disk_usage_for(media_dir)
    return SystemSnapshot(
        queue_depth=int(queue_depth),
        oldest_queued_minutes=int((now - oldest).total_seconds() // 60) if oldest else 0,
        inbox_backlog=int(inbox_backlog),
        outbox_pending=int(outbox_pending),
        failed_24h=int(failed_24h),
        disk_total_bytes=usage.total,
        disk_free_bytes=usage.free,
        last_backup_at=last_backup,
    )


async def check_provider(
    sessionmaker: async_sessionmaker[AsyncSession], provider: WhatsAppProvider | None
) -> list[str]:
    """Ask the provider about ITSELF and alert on what it says.

    Three failures live only here, and each one makes the bot silent
    while every internal number looks healthy:
    * the instance is configured to deliver notifications to a webhook,
      so our polling queue is empty forever;
    * notifications pile up on the provider's side because we are not
      collecting them;
    * our sends sit in the provider's outgoing queue (up to 24 hours)
      because the instance is not authorized.
    """
    if provider is None:
        return []
    raised: list[str] = []
    try:
        health = await provider.get_health()
    except Exception:
        logger.exception("provider_health_failed")
        return []

    async with sessionmaker() as db:
        bucket = hour_bucket()
        depth_limit = int(await get_setting(db, app_settings.PROVIDER_QUEUE_ALERT_DEPTH))

        if health.settings:
            instance = (
                await db.execute(
                    select(WhatsappInstance)
                    .where(WhatsappInstance.is_active.is_(True))
                    .order_by(WhatsappInstance.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if instance is not None:
                instance.settings_json = dict(health.settings)
                instance.settings_checked_at = datetime.now(UTC)

        if health.settings_problems and await record_alert(
            db,
            kind="system",
            event_key="instance_misconfigured",
            period_key=datetime.now(UTC).date().isoformat(),
            detail={"problems": health.settings_problems[:3]},
        ):
            raised.append("instance_misconfigured")
            db.add(
                SystemEvent(
                    level="critical",
                    component="monitoring",
                    event="instance_misconfigured",
                    detail={"problems": health.settings_problems},
                )
            )

        if (
            health.incoming_queue is not None
            and health.incoming_queue >= depth_limit
            and await record_alert(
                db,
                kind="system",
                event_key="provider_incoming_queue",
                period_key=bucket,
                detail={"depth": health.incoming_queue},
            )
        ):
            raised.append("provider_incoming_queue")
            db.add(
                SystemEvent(
                    level="warning",
                    component="monitoring",
                    event="provider_incoming_queue",
                    detail={"depth": health.incoming_queue},
                )
            )

        if (
            health.outgoing_queue is not None
            and health.outgoing_queue >= depth_limit
            and await record_alert(
                db,
                kind="system",
                event_key="provider_outgoing_queue",
                period_key=bucket,
                detail={"depth": health.outgoing_queue},
            )
        ):
            raised.append("provider_outgoing_queue")
            db.add(
                SystemEvent(
                    level="error",
                    component="monitoring",
                    event="provider_outgoing_queue",
                    detail={"depth": health.outgoing_queue, "socket": health.socket},
                )
            )
        await db.commit()
    if raised:
        logger.warning("provider_alerts", raised=raised)
    return raised


async def check_and_alert(
    sessionmaker: async_sessionmaker[AsyncSession], *, media_dir: str
) -> list[str]:
    """One monitoring pass. Returns the alert keys it raised."""
    raised: list[str] = []
    async with sessionmaker() as db:
        snapshot = await collect(db, media_dir=media_dir)
        disk_limit = int(await get_setting(db, app_settings.DISK_ALERT_PERCENT))
        queue_limit = int(await get_setting(db, app_settings.QUEUE_ALERT_DEPTH))
        backup_hours = int(await get_setting(db, app_settings.BACKUP_STALE_HOURS))
        bucket = hour_bucket()

        if snapshot.disk_used_percent >= disk_limit:
            free_percent = 100 - snapshot.disk_used_percent
            if await record_alert(
                db,
                kind="system",
                event_key="disk",
                period_key=bucket,
                detail={"free_percent": free_percent, "used_percent": snapshot.disk_used_percent},
            ):
                raised.append("disk")
                db.add(
                    SystemEvent(
                        level="critical",
                        component="monitoring",
                        event="disk_low",
                        detail={"used_percent": snapshot.disk_used_percent},
                    )
                )

        if snapshot.queue_depth >= queue_limit and await record_alert(
            db,
            kind="system",
            event_key="queue",
            period_key=bucket,
            detail={
                "depth": snapshot.queue_depth,
                "oldest_minutes": snapshot.oldest_queued_minutes,
            },
        ):
            raised.append("queue")
            db.add(
                SystemEvent(
                    level="warning",
                    component="monitoring",
                    event="queue_growing",
                    detail={
                        "depth": snapshot.queue_depth,
                        "oldest_minutes": snapshot.oldest_queued_minutes,
                    },
                )
            )

        # Notifications arriving faster than they are parsed: the AI
        # pipeline never even sees these messages, so «ждут ответа ИИ»
        # can look healthy while customers are not being read at all.
        if snapshot.inbox_backlog >= queue_limit and await record_alert(
            db,
            kind="system",
            event_key="queue",
            period_key=f"{bucket}:inbox",
            detail={"depth": snapshot.inbox_backlog},
        ):
            raised.append("inbox_backlog")
            db.add(
                SystemEvent(
                    level="warning",
                    component="monitoring",
                    event="inbox_backlog",
                    detail={"depth": snapshot.inbox_backlog},
                )
            )

        age = snapshot.backup_age_hours
        # No dump has EVER been recorded: that is the fresh-install state,
        # not a failure — the owner installs the cron job during the
        # deployment checklist. Alerting here would cry wolf on day one.
        # Daily period key: a stale backup is a "tell me once a day"
        # problem, not an hourly one.
        if (
            age is not None
            and age >= backup_hours
            and await record_alert(
                db,
                kind="system",
                event_key="backup_stale",
                period_key=datetime.now(UTC).date().isoformat(),
                detail={"hours": age},
            )
        ):
            raised.append("backup_stale")
            db.add(
                SystemEvent(
                    level="error",
                    component="monitoring",
                    event="backup_stale",
                    detail={"hours": age},
                )
            )
        await db.commit()
    if raised:
        logger.warning("monitoring_alerts", raised=raised)
    return raised
