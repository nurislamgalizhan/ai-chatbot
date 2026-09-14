"""WhatsApp alerts to the owner (owner decision §4.7: no Telegram).

Four rules, each of them learned the hard way somewhere else:

1. **An alert is not a customer reply.** It travels down the same
   transport (`wa_outbox`, so it inherits idempotency and retries) but
   carries `purpose='alert'`, and DRY_RUN mutes only the customer
   channel. "The daily budget is spent, the bot went quiet" is exactly
   the message that must arrive during acceptance — that is the point of
   watching a bot that is not sending anything yet.
2. **Nothing is sent until the owner asks for it.** `alert_phone` is
   empty by default, and while it is empty alerts are only recorded and
   shown in «Система». Filling it in is the owner's explicit consent to
   receive WhatsApp messages from the bot on that number.
3. **Once per thing per period.** The UNIQUE index on
   (kind, event_key, period_key) is the dedup, not a variable in this
   process: a restart must not re-alert, and two workers must not both.
4. **The owner's own number never becomes a customer.** Alerts do not
   create chats, and incoming messages from that number are ignored by
   the inbox (see `is_alert_chat`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import AlertNotification, SystemEvent, WaOutbox, WhatsappInstance
from app.services import app_settings
from app.services.app_settings import get_setting
from app.services.phone import normalize_phone
from app.whatsapp.outbox import ALERT_PURPOSE, enqueue_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger(__name__)

#: Alerts queued per pass. The retry schedule itself belongs to the
#: outbox (bounded attempts with backoff), not to a second mechanism here.
_SEND_BATCH = 5
#: How long an alert waits for a destination before it is retired. Long
#: enough that filling in the number later still delivers what happened
#: today; short enough that a permanently empty setting does not build a
#: queue of ancient news.
_UNDELIVERABLE_GRACE = timedelta(days=3)


def wa_chat_id_for(phone: str) -> str:
    """`+7 701 123-45-67` -> `77011234567@c.us` (GREEN-API addressing)."""
    return f"{normalize_phone(phone).lstrip('+')}@c.us"


def is_alert_chat(wa_chat_id: str, alert_phone: str | None) -> bool:
    """True when this WhatsApp chat is the owner's alert channel.

    Used by the inbox: without it, our own alert echoes back as an
    outgoing notification and creates a "customer" chat with the owner,
    and the owner's reply to an alert would be answered by the AI — paid
    for, and counted as a customer conversation.
    """
    if not alert_phone:
        return False
    return wa_chat_id.strip().lower() == wa_chat_id_for(alert_phone).lower()


# --------------------------- composing text ------------------------------

#: Owner-facing wording. White-label: no company name, no customer text,
#: no phone numbers of customers — an alert is about the SYSTEM.
_TEXTS: dict[str, str] = {
    "budget_day_50pct": "Израсходовано 50% дневного бюджета ИИ.",
    "budget_day_80pct": "Израсходовано 80% дневного бюджета ИИ.",
    "budget_day_90pct": "Израсходовано 90% дневного бюджета ИИ. Скоро бот перестанет отвечать сам.",
    "budget_day_100pct": (
        "Дневной бюджет ИИ исчерпан. Бот больше не отвечает сам до конца суток (UTC) — "
        "диалоги уходят менеджеру."
    ),
    "budget_month_50pct": "Израсходовано 50% месячного бюджета ИИ.",
    "budget_month_80pct": "Израсходовано 80% месячного бюджета ИИ.",
    "budget_month_90pct": "Израсходовано 90% месячного бюджета ИИ.",
    "budget_month_100pct": (
        "Месячный бюджет ИИ исчерпан. Бот больше не отвечает сам до конца месяца — "
        "диалоги уходят менеджеру."
    ),
    # Raised when a RESERVATION is refused — the moment the bot actually
    # stops answering, which can happen before any percentage threshold
    # (the next estimate simply no longer fits under the limit).
    "budget_block_global_day": (
        "Дневной лимит расходов ИИ достигнут — бот перестал отвечать сам, диалоги уходят менеджеру."
    ),
    "budget_block_global_month": (
        "Месячный лимит расходов ИИ достигнут — бот перестал отвечать сам, "
        "диалоги уходят менеджеру."
    ),
    "budget_block_chat": (
        "Достигнут лимит расходов на один диалог: этот диалог передан менеджеру. "
        "Лимит меняется в разделе «Лимиты»."
    ),
    "budget_block_user_day": (
        "Клиент исчерпал дневной лимит расходов на одного человека — его диалог передан менеджеру."
    ),
    "system_circuit_breaker": (
        "ИИ временно отключён: подряд несколько ошибок OpenAI. Бот передаёт диалоги менеджеру "
        "и сам попробует снова через несколько минут."
    ),
    "system_poll_failed": (
        "Нет связи с WhatsApp (GREEN-API): входящие сообщения сейчас не забираются. "
        "Проверьте статус инстанса в панели GREEN-API."
    ),
    "system_disk": "На сервере заканчивается место на диске.",
    "system_instance_misconfigured": (
        "Бот не получает сообщения из-за настроек инстанса WhatsApp. Откройте раздел "
        "«Система» — там написано, что именно исправить в панели GREEN-API."
    ),
    "system_provider_incoming_queue": (
        "У провайдера накопились непрочитанные уведомления — бот не успевает их "
        "забирать или не получает их вовсе."
    ),
    "system_provider_outgoing_queue": (
        "Ответы клиентам зависли в очереди у провайдера и не доставлены. Обычно это "
        "значит, что номер WhatsApp разлогинился."
    ),
    "system_quota_exceeded": (
        "Исчерпан лимит тарифа GREEN-API: сообщения клиентам перестали доставляться. "
        "Проверьте тариф в панели GREEN-API."
    ),
    "system_yellow_card": (
        "WhatsApp ограничил номер за подозрительную активность (yellow card). Есть "
        "риск блокировки номера — остановите рассылки и проверьте переписку."
    ),
    "system_queue": "Очередь необработанных сообщений растёт — бот не успевает отвечать.",
    "system_backup_stale": "Резервная копия базы давно не создавалась.",
    "system_not_configured": (
        "Бот не может отвечать: не хватает настройки (ключ, инструкция или цены)."
    ),
    "system_test": "Проверочное сообщение из админ-панели. Алерты настроены верно.",
}


def alert_text(kind: str, event_key: str, detail: dict[str, Any] | None) -> str:
    """The sentence the owner receives. Falls back to a plain statement
    rather than a code, so an unforeseen alert is still readable."""
    key = f"{kind}_{event_key}" if kind.startswith("budget") else f"system_{event_key}"
    base = _TEXTS.get(key) or _TEXTS.get(f"system_{event_key}") or f"Событие системы: {event_key}."
    extra = _detail_suffix(kind, detail or {})
    return f"{base}{extra}" if extra else base


def _detail_suffix(kind: str, detail: dict[str, Any]) -> str:
    if kind.startswith("budget"):
        spent, limit = detail.get("spent_kzt"), detail.get("limit_kzt")
        if spent is not None and limit is not None:
            return f" Потрачено {_money(spent)} ₸ из {_money(limit)} ₸."
        return ""
    if "free_percent" in detail:
        return f" Свободно {detail['free_percent']}%."
    if "depth" in detail:
        return f" В очереди {detail['depth']}."
    if "hours" in detail:
        return f" Последняя копия: {detail['hours']} ч назад."
    return ""


def _money(value: Any) -> str:  # noqa: ANN401 - JSONB detail values
    try:
        amount = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return str(value)
    quantized = amount.quantize(Decimal("0.01")) if amount < 100 else amount.quantize(Decimal("1"))  # noqa: PLR2004
    return f"{quantized:,}".replace(",", " ")


# --------------------------- recording alerts ----------------------------


async def record_alert(
    db: AsyncSession,
    *,
    kind: str,
    event_key: str,
    period_key: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Queue an alert once. Returns True if THIS call created it.

    `period_key` is what "once" means: a date for a daily budget, a month
    for a monthly one, an hour bucket for a recurring technical failure
    (a broken WhatsApp connection retries every few seconds — without a
    bucket the owner would get a message per retry).
    """
    inserted = (
        await db.execute(
            pg_insert(AlertNotification)
            .values(
                kind=kind,
                event_key=event_key,
                period_key=period_key,
                detail=detail,
                body=alert_text(kind, event_key, detail),
                status="pending",
            )
            .on_conflict_do_nothing(constraint="uq_alert_notifications_dedupe")
            .returning(AlertNotification.id)
        )
    ).scalar_one_or_none()
    return inserted is not None


def hour_bucket(now: datetime | None = None) -> str:
    """Period key for technical alerts: at most one per hour per kind."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H")


# --------------------------- sending alerts ------------------------------

#: Deterministic outbound id per alert: a retry of the queueing step can
#: never produce a second WhatsApp message for the same alert.
_ALERT_NAMESPACE = uuid.UUID("5f3b0b2e-8a1c-4f6e-9c2a-9a1d4c7e0b11")


def alert_outbound_id(alert_id: int) -> uuid.UUID:
    return uuid.uuid5(_ALERT_NAMESPACE, f"alert:{alert_id}")


@dataclass(slots=True)
class AlertDeliveryResult:
    queued: int = 0
    settled: int = 0
    skipped: int = 0


class AlertDispatcher:
    """Moves pending alerts onto the transport and reads back the result.

    Delivery itself is the outbox's job — it already has the idempotency
    barrier, the retry schedule and the single place that talks to the
    provider. This class owns only the two edges: what to queue, and what
    the outcome means for the alert row the owner sees in «Система».
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def dispatch_pending(self) -> AlertDeliveryResult:
        result = AlertDeliveryResult()
        now = datetime.now(UTC)
        async with self._sessionmaker() as db:
            enabled = bool(await get_setting(db, app_settings.ALERTS_ENABLED))
            phone = str(await get_setting(db, app_settings.ALERT_PHONE) or "").strip()
            instance = (
                await db.execute(
                    select(WhatsappInstance.id)
                    .where(WhatsappInstance.is_active.is_(True))
                    .order_by(WhatsappInstance.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            pending = (
                (
                    await db.execute(
                        select(AlertNotification)
                        .where(AlertNotification.status == "pending")
                        .order_by(AlertNotification.id)
                        .limit(_SEND_BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not pending:
                await self._settle_queued(db)
                await db.commit()
                return result

            if not enabled or not phone or instance is None:
                # Recorded and visible in «Система», deliberately not
                # sent: no number given (the owner has not opted in), or
                # alerts switched off, or no transport configured at all.
                #
                # They stay PENDING rather than being marked skipped: the
                # dedup is a UNIQUE constraint, so a burned alert could
                # never be raised again for that period — the owner would
                # fill in their number and never hear about the budget
                # that was already spent. Only alerts older than the
                # grace window are retired, and they say why.
                reason = (
                    "alerts_off" if not enabled else ("no_phone" if not phone else "no_instance")
                )
                cutoff = now - _UNDELIVERABLE_GRACE
                for row in pending:
                    row.last_error = reason
                    if row.created_at < cutoff:
                        row.status = "skipped"
                        row.next_attempt_at = None
                        result.skipped += 1
                await db.commit()
                return result

            chat_id = wa_chat_id_for(phone)
            for row in pending:
                outbound_id = alert_outbound_id(row.id)
                await enqueue_text(
                    db,
                    instance,
                    chat_id,
                    row.body or alert_text(row.kind, row.event_key, row.detail),
                    outbound_id,
                    f"alert:{row.id}",
                    purpose=ALERT_PURPOSE,
                )
                row.status = "queued"
                row.outbound_message_id = outbound_id
                row.attempts = row.attempts + 1
                result.queued += 1
            result.settled = await self._settle_queued(db)
            await db.commit()
        return result

    async def _settle_queued(self, db: AsyncSession) -> int:
        """Copy terminal transport states onto the alerts themselves."""
        rows = (
            await db.execute(
                select(AlertNotification, WaOutbox)
                .join(
                    WaOutbox,
                    WaOutbox.outbound_message_id == AlertNotification.outbound_message_id,
                )
                .where(
                    AlertNotification.status == "queued",
                    WaOutbox.state.in_(("SENT", "FAILED_FINAL", "SKIPPED_DRY_RUN")),
                )
            )
        ).all()
        for alert, outbox_row in rows:
            if outbox_row.state == "SENT":
                alert.status = "sent"
                alert.sent_at = outbox_row.sent_at
                alert.last_error = None
            elif outbox_row.state == "SKIPPED_DRY_RUN":
                # Only possible if the purpose was rewritten by hand;
                # recorded honestly rather than shown as delivered.
                alert.status = "skipped"
                alert.last_error = "dry_run"
            else:
                alert.status = "failed"
                alert.last_error = outbox_row.error_code
                db.add(
                    SystemEvent(
                        level="error",
                        component="alerts",
                        event="alert_delivery_failed",
                        detail={"kind": alert.kind, "event_key": alert.event_key},
                    )
                )
            alert.next_attempt_at = None
        return len(rows)
