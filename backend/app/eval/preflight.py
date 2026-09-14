"""Pre-deployment checks: is this installation able to answer a customer?

Run by the owner (`make preflight`) before switching DRY_RUN off and
after every update. Every check is read-only and free — nothing here
calls OpenAI (invariant 11: opening things must not cost money); the
provider is asked about itself only with `--with-provider`, because
those requests count against the owner's GREEN-API plan.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    AdminUser,
    AgentInstruction,
    FxRate,
    KbDocument,
    KbFaq,
    ModelPrice,
    SystemEvent,
    WhatsappInstance,
)
from app.models.enums import AnswerStatus, DocStatus, PriceUnit
from app.services import app_settings
from app.services.app_settings import get_setting
from app.services.monitoring import BACKUP_EVENT

if TYPE_CHECKING:
    from app.config import Settings

Status = Literal["ok", "warn", "fail", "info"]
_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
_BACKEND_DIR = Path(__file__).resolve().parents[2]
#: The worker's liveness key (kept in sync with app.worker.main).
HEARTBEAT_KEY = "worker:heartbeat"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass
class PreflightReport:
    started_at: datetime
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    def add(self, name: str, status: Status, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ok": self.ok,
            "checks": [asdict(c) for c in self.checks],
        }

    def render_markdown(self) -> str:
        fails = sum(1 for c in self.checks if c.status == "fail")
        warns = sum(1 for c in self.checks if c.status == "warn")
        lines = [
            "# Проверка готовности (preflight)",
            "",
            f"- Время: {self.started_at:%Y-%m-%d %H:%M} UTC",
            f"- Итог: **{'можно запускать' if self.ok else 'есть блокирующие проблемы'}** "
            f"(ошибок: {fails}, предупреждений: {warns})",
            "",
            "| Проверка | Статус | Подробности |",
            "|---|---|---|",
        ]
        lines += [f"| {c.name} | {_ICON[c.status]} | {c.detail} |" for c in self.checks]
        return "\n".join(lines) + "\n"


def _alembic_head() -> str | None:
    try:
        from alembic.config import Config  # noqa: PLC0415 - CLI-time dependency
        from alembic.script import ScriptDirectory  # noqa: PLC0415

        cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # noqa: BLE001 - reported as a check, not a crash
        return None


async def _check_database(db: AsyncSession, report: PreflightReport) -> None:
    current = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    head = _alembic_head()
    if head is None:
        report.add("Миграции", "warn", "не удалось прочитать историю Alembic")
    elif current == head:
        report.add("Миграции", "ok", f"схема на последней ревизии {head}")
    else:
        report.add("Миграции", "fail", f"в базе {current}, в коде {head} — выполните make migrate")


async def _check_admin(db: AsyncSession, report: PreflightReport) -> None:
    active = (
        await db.execute(
            select(func.count()).select_from(AdminUser).where(AdminUser.is_active.is_(True))
        )
    ).scalar_one()
    if active == 1:
        report.add("Учётная запись", "ok", "одна активная учётная запись")
    elif active == 0:
        report.add("Учётная запись", "fail", "учётной записи нет — выполните make create-admin")
    else:
        report.add(
            "Учётная запись", "fail", f"активных учётных записей: {active}, должна быть одна"
        )


async def _check_money(db: AsyncSession, settings: Settings, report: PreflightReport) -> None:
    rate = (
        await db.execute(select(FxRate).order_by(FxRate.effective_from.desc()).limit(1))
    ).scalar_one_or_none()
    if rate is None:
        report.add(
            "Курс USD/KZT",
            "fail",
            "курс не задан — без него ИИ не тратит (fail closed); введите в «Лимитах»",
        )
    else:
        report.add("Курс USD/KZT", "ok", f"{rate.rate} ₸ за 1 $")
    required = (
        (settings.openai_chat_model, PriceUnit.PER_1M_INPUT),
        (settings.openai_chat_model, PriceUnit.PER_1M_CACHED_INPUT),
        (settings.openai_chat_model, PriceUnit.PER_1M_OUTPUT),
        (settings.openai_embedding_model, PriceUnit.PER_1M_INPUT),
        (settings.openai_transcribe_model, PriceUnit.PER_MINUTE_AUDIO),
    )
    missing: list[str] = []
    for model, unit in required:
        exists = (
            await db.execute(
                select(ModelPrice.id)
                .where(ModelPrice.model == model, ModelPrice.unit == unit)
                .limit(1)
            )
        ).scalar_one_or_none()
        if exists is None:
            missing.append(f"{model} {unit.value}")
    if missing:
        report.add("Цены моделей", "fail", "нет цен: " + ", ".join(missing) + " — make seed-prices")
    else:
        report.add("Цены моделей", "ok", "заведены для чата, эмбеддингов и распознавания речи")
    daily = await get_setting(db, app_settings.DAILY_AI_BUDGET_KZT)
    monthly = await get_setting(db, app_settings.MONTHLY_AI_BUDGET_KZT)
    report.add("Бюджет", "info", f"день {daily} ₸, месяц {monthly} ₸ (правится в «Лимитах»)")


async def _check_instruction(db: AsyncSession, report: PreflightReport) -> None:
    active = (
        await db.execute(select(AgentInstruction).where(AgentInstruction.is_active.is_(True)))
    ).scalar_one_or_none()
    if active is None:
        report.add(
            "Инструкция агента", "fail", "нет активной версии — создайте и активируйте в «Агенте»"
        )
    else:
        report.add("Инструкция агента", "ok", f"активна версия {active.version}")


async def _check_knowledge(db: AsyncSession, report: PreflightReport) -> None:
    rows = (
        await db.execute(
            select(
                KbDocument.status, KbDocument.is_searchable, KbDocument.index_state, func.count()
            )
            .where(KbDocument.deleted_at.is_(None))
            .group_by(KbDocument.status, KbDocument.is_searchable, KbDocument.index_state)
        )
    ).all()
    total = sum(int(n) for *_, n in rows)
    indexed = sum(int(n) for status, searchable, state, n in rows if state == "indexed")
    pending = sum(int(n) for *_, state, n in rows if state == "pending")
    failed = sum(int(n) for *_, state, n in rows if state == "failed")
    drafts = sum(int(n) for status, *_, n in rows if status is DocStatus.DRAFT)
    if total == 0:
        report.add("Справка для бота", "warn", "документов нет — бот сможет отвечать только по FAQ")
    elif indexed == 0:
        report.add(
            "Справка для бота",
            "warn",
            f"документов {total}, черновиков {drafts}, ни один не выучен — "
            "активируйте документы в «Базе знаний»",
        )
    else:
        report.add(
            "Справка для бота",
            "ok" if not failed else "warn",
            f"выучено {indexed} из {total}, черновиков {drafts}, в очереди {pending}, "
            f"с ошибкой {failed}",
        )
    faqs = (await db.execute(select(KbFaq).where(KbFaq.is_active.is_(True)))).scalars().all()
    if not faqs:
        report.add(
            "Вопрос — ответ",
            "warn",
            "нет активных записей — частые вопросы пойдут в модель за деньги",
        )
    else:
        no_kk = sum(1 for f in faqs if f.answer_kk_status is not AnswerStatus.APPROVED)
        no_en = sum(1 for f in faqs if f.answer_en_status is not AnswerStatus.APPROVED)
        detail = f"активных записей {len(faqs)}"
        if no_kk or no_en:
            detail += (
                f"; без утверждённого казахского {no_kk}, английского {no_en} — "
                "клиенты на этих языках получат ответ модели, не FAQ"
            )
        report.add("Вопрос — ответ", "ok" if not no_kk else "warn", detail)


async def _check_alerts(db: AsyncSession, report: PreflightReport) -> None:
    phone = str(await get_setting(db, app_settings.ALERT_PHONE) or "")
    enabled = bool(await get_setting(db, app_settings.ALERTS_ENABLED))
    if not phone:
        report.add(
            "Оповещения владельцу",
            "warn",
            "номер не указан — оповещения копятся в «Системе» и никуда не уходят",
        )
    elif not enabled:
        report.add("Оповещения владельцу", "warn", "выключены в «Лимитах»")
    else:
        report.add("Оповещения владельцу", "ok", f"номер задан (…{phone[-4:]}), включены")
    if bool(await get_setting(db, app_settings.EMERGENCY_AI_OFF)):
        report.add(
            "Аварийный выключатель ИИ", "warn", "ИИ выключен глобально — бот не отвечает никому"
        )
    else:
        report.add("Аварийный выключатель ИИ", "ok", "ИИ включён")


async def _check_backup(db: AsyncSession, report: PreflightReport) -> None:
    last = (
        await db.execute(
            select(func.max(SystemEvent.created_at)).where(
                SystemEvent.component == "backup", SystemEvent.event == BACKUP_EVENT
            )
        )
    ).scalar()
    stale_hours = int(await get_setting(db, app_settings.BACKUP_STALE_HOURS))
    if last is None:
        report.add(
            "Резервные копии",
            "info",
            "ещё ни одной — установите таймер (deploy/systemd) и запустите make backup",
        )
        return
    age = (datetime.now(UTC) - last).total_seconds() / 3600
    if age >= stale_hours:
        report.add(
            "Резервные копии", "warn", f"последняя {age:.0f} ч назад (порог {stale_hours} ч)"
        )
    else:
        report.add("Резервные копии", "ok", f"последняя {age:.0f} ч назад")


async def _check_transport(
    db: AsyncSession, settings: Settings, report: PreflightReport, *, with_provider: bool
) -> None:
    if not settings.green_api_id_instance:
        report.add("GREEN-API", "warn", "инстанс не настроен в .env — сообщения не принимаются")
        return
    instance = (
        await db.execute(
            select(WhatsappInstance).where(WhatsappInstance.is_active.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if instance is None:
        report.add(
            "GREEN-API", "warn", "инстанс задан в .env, но воркер ещё не зарегистрировал его в базе"
        )
    else:
        detail = f"инстанс …{settings.green_api_id_instance[-4:]}"
        if instance.state:
            detail += f", состояние «{instance.state}»"
        if instance.settings_json and instance.settings_checked_at:
            detail += f", настройки проверены {instance.settings_checked_at:%Y-%m-%d %H:%M}"
        report.add("GREEN-API", "ok", detail)
    if not with_provider:
        report.add(
            "Настройки инстанса",
            "info",
            "не запрашивались (запуск с --with-provider тратит запросы тарифа)",
        )
        return
    await _check_provider_live(settings, report)


async def _check_provider_live(settings: Settings, report: PreflightReport) -> None:
    """Ask GREEN-API about the instance: paid requests, opt-in only."""
    from app.whatsapp.green_api import GreenApiProvider  # noqa: PLC0415 - optional, paid requests

    provider = GreenApiProvider(settings)
    try:
        state = await provider.get_instance_state()
        health = await provider.get_health()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        report.add("Настройки инстанса", "fail", f"провайдер не ответил: {type(exc).__name__}")
        return
    finally:
        await provider.aclose()
    if state != "authorized":
        report.add(
            "Состояние инстанса",
            "fail",
            f"«{state}» — номер не авторизован, отсканируйте QR в панели GREEN-API",
        )
    else:
        report.add(
            "Состояние инстанса",
            "ok",
            "authorized" + (f", сокет {health.socket}" if health.socket else ""),
        )
    if health.settings_problems:
        for problem in health.settings_problems:
            report.add("Настройки инстанса", "fail", problem)
    else:
        report.add("Настройки инстанса", "ok", "опрос уведомлений настроен верно")
    if health.incoming_queue:
        report.add(
            "Очередь провайдера",
            "warn",
            f"на стороне провайдера ждут {health.incoming_queue} уведомлений",
        )
    if health.outgoing_queue:
        report.add(
            "Недоставленные отправки",
            "warn",
            f"у провайдера лежат {health.outgoing_queue} наших сообщений",
        )


def _check_env(settings: Settings, report: PreflightReport) -> None:
    report.add(
        "Окружение",
        "ok" if settings.app_env == "prod" else "warn",
        f"APP_ENV={settings.app_env}"
        + ("" if settings.app_env == "prod" else " — для продакшена нужен prod"),
    )
    if settings.dry_run:
        report.add("DRY_RUN", "info", "включён: ответы клиентам не отправляются (режим приёмки)")
    else:
        report.add("DRY_RUN", "warn", "выключен: бот отправляет сообщения реальным клиентам")
    if settings.openai_api_key:
        report.add("Ключ OpenAI", "ok", f"задан, модель {settings.openai_chat_model}")
    else:
        report.add(
            "Ключ OpenAI",
            "warn",
            "не задан — бот не отвечает, вопросы ждут в очереди и уходят менеджеру",
        )
    origins = settings.cors_origin_list
    report.add(
        "Домен панели (CORS)",
        "ok" if origins else ("warn" if settings.app_env != "prod" else "fail"),
        ", ".join(origins) if origins else "не задан",
    )


async def _check_disk(db: AsyncSession, settings: Settings, report: PreflightReport) -> None:
    path = Path(settings.media_dir)
    if not path.exists():
        report.add(
            "Диск", "warn", f"каталог медиа {settings.media_dir} недоступен из этого процесса"
        )
        return
    usage = shutil.disk_usage(path)
    used_percent = int((usage.total - usage.free) / usage.total * 100) if usage.total else 0
    limit = int(await get_setting(db, app_settings.DISK_ALERT_PERCENT))
    status: Status = "fail" if used_percent >= limit else ("warn" if used_percent >= 70 else "ok")  # noqa: PLR2004
    report.add("Диск", status, f"занято {used_percent} %, свободно {usage.free / 1024**3:.1f} ГБ")


async def run_preflight(
    settings: Settings, *, with_provider: bool = False, redis: Redis | None = None
) -> PreflightReport:
    """All checks; `redis` may be injected (tests use a fake)."""
    report = PreflightReport(started_at=datetime.now(UTC))
    _check_env(settings, report)

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            report.add("База данных", "ok", "подключение есть")
        except Exception as exc:  # noqa: BLE001 - the whole point is to report it
            report.add("База данных", "fail", f"нет подключения: {type(exc).__name__}")
            return report
        async with engine.connect() as conn, AsyncSession(bind=conn) as db:
            await _check_database(db, report)
            await _check_admin(db, report)
            await _check_instruction(db, report)
            await _check_money(db, settings, report)
            await _check_knowledge(db, report)
            await _check_alerts(db, report)
            await _check_backup(db, report)
            await _check_transport(db, settings, report, with_provider=with_provider)
            await _check_disk(db, settings, report)
    finally:
        await engine.dispose()

    redis = redis if redis is not None else Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        report.add("Redis", "ok", "подключение есть")
        heartbeat = await redis.get(HEARTBEAT_KEY)
        if heartbeat:
            report.add("Воркер", "ok", "сердцебиение свежее")
        else:
            report.add("Воркер", "warn", "сердцебиения нет — воркер не запущен или не отвечает")
    except Exception as exc:  # noqa: BLE001
        report.add("Redis", "fail", f"нет подключения: {type(exc).__name__}")
    finally:
        await redis.aclose()
    return report
