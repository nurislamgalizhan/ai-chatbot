"""Management commands. Passwords are read from stdin (getpass), never from
argv — command-line arguments end up in shell history."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import get_settings
from app.db.engine import build_engine, build_sessionmaker
from app.observability.logging import configure_logging
from app.services.admin_setup import (
    AdminSetupError,
    create_admin,
    reset_admin_password,
)
from app.services.kb_import import import_savvi_export
from app.services.phone import PhoneValidationError
from app.services.pricing import seed_default_prices

if TYPE_CHECKING:
    from app.config import Settings


def _read_new_password() -> str:
    password = getpass.getpass("Пароль (мин. 12 символов): ")
    confirm = getpass.getpass("Пароль ещё раз: ")
    if password != confirm:
        print("Пароли не совпадают.", file=sys.stderr)
        raise SystemExit(2)
    return password


async def _cmd_create_admin() -> int:
    settings = get_settings()
    phone = input("Телефон (E.164, например +77011234567): ").strip()
    full_name = input("Имя: ").strip()
    password = _read_new_password()

    engine = build_engine(settings)
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            created = await create_admin(db, settings, phone, full_name, password)
    except (AdminSetupError, PhoneValidationError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    print("\nУчётная запись создана.")
    print(f"Телефон: {created.phone}")
    print("\nНастройте приложение-аутентификатор (TOTP):")
    print(f"  URI: {created.totp_uri}")
    print(f"  Секрет (ввести вручную): {created.totp_secret}")
    print("\nКоды восстановления — показываются ОДИН раз, сохраните их:")
    for code in created.recovery_codes:
        print(f"  {code}")
    print(
        "\nБез приложения-аутентификатора и без кодов восстановления доступ "
        "к админке восстановить нельзя."
    )
    return 0


async def _cmd_reset_admin_password() -> int:
    settings = get_settings()
    password = _read_new_password()
    engine = build_engine(settings)
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            phone = await reset_admin_password(db, password)
    except AdminSetupError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
    print(f"Пароль для {phone} обновлён. Все сессии завершены.")
    return 0


async def _cmd_kb_import(source: str) -> int:
    settings = get_settings()
    path = Path(source)
    if not path.is_file():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 2
    engine = build_engine(settings)
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            report = await import_savvi_export(db, path)
    finally:
        await engine.dispose()
    print(report.render())
    print(
        "\nВсе документы созданы как черновики и не индексируются. "
        "Проверьте их в админке и включите нужные."
    )
    return 0


async def _cmd_seed_prices() -> int:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            created = await seed_default_prices(db, settings)
    finally:
        await engine.dispose()
    if not created:
        print("Все цены уже заведены — ничего не изменено.")
        return 0
    print("Заведены стартовые цены (SPEC §9.1):")
    for line in created:
        print(f"  {line}")
    print(
        "\nВНИМАНИЕ: сверьте цены с актуальной страницей цен OpenAI и при "
        "расхождении поправьте их в админке (раздел «Лимиты»)."
    )
    return 0


# ------------------------------ acceptance (phase 10) ------------------------------


def _quiet_logging(settings: Settings) -> None:
    """Reports go to stdout; logs must not be mixed into them."""
    configure_logging(settings.model_copy(update={"log_level": "WARNING"}), stream=sys.stderr)


def _emit(
    markdown: str, data: dict[str, object], *, out: Path | None, stem: str, as_json: bool
) -> None:
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        (out / f"{stem}-{stamp}.md").write_text(markdown, encoding="utf-8")
        (out / f"{stem}-{stamp}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"Отчёт записан: {out / f'{stem}-{stamp}.md'}", file=sys.stderr)
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(markdown, end="")


async def _cmd_eval(
    *, live: bool, out: Path | None, cases: Path, keep_db: bool, yes: bool, as_json: bool
) -> int:
    from app.eval.commands import (  # noqa: PLC0415
        confirm_live_eval,
        run_live_eval,
        run_offline_eval,
    )
    from app.eval.runner import load_suite  # noqa: PLC0415

    settings = get_settings()
    _quiet_logging(settings)
    suite = load_suite(cases)
    if live:
        if not settings.openai_api_key:
            print("Живой прогон невозможен: в .env нет OPENAI_API_KEY.", file=sys.stderr)
            return 2
        if not confirm_live_eval(settings, suite, yes=yes):
            print("Отменено.", file=sys.stderr)
            return 1
        report = await run_live_eval(settings, suite)
    else:
        report = await run_offline_eval(settings, suite, keep_db=keep_db)
    _emit(
        report.render_markdown(),
        report.to_dict(),
        out=out,
        stem=f"eval-{'live' if live else 'offline'}",
        as_json=as_json,
    )
    return 1 if report.mandatory_failed else 0


async def _cmd_load_test(
    *,
    chats: list[int],
    voice_every: int,
    latency: float,
    out: Path | None,
    keep_db: bool,
    as_json: bool,
) -> int:
    from app.eval.commands import run_load_test  # noqa: PLC0415
    from app.eval.runner import load_suite  # noqa: PLC0415

    settings = get_settings()
    _quiet_logging(settings)
    report = await run_load_test(
        settings,
        load_suite(),
        chats=chats,
        voice_every=voice_every,
        latency=latency,
        keep_db=keep_db,
    )
    _emit(report.render_markdown(), report.to_dict(), out=out, stem="load-test", as_json=as_json)
    return 0 if report.ok else 1


async def _cmd_preflight(*, with_provider: bool, as_json: bool) -> int:
    from app.eval.preflight import run_preflight  # noqa: PLC0415

    settings = get_settings()
    _quiet_logging(settings)
    report = await run_preflight(settings, with_provider=with_provider)
    _emit(report.render_markdown(), report.to_dict(), out=None, stem="preflight", as_json=as_json)
    return 0 if report.ok else 1


def _int_list(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create the single admin account")
    sub.add_parser("reset-admin-password", help="Reset the admin password")
    kb_import = sub.add_parser(
        "kb-import", help="Import the Savvi export into the knowledge base (drafts)"
    )
    kb_import.add_argument("--source", default="seed/savvi-export.md")
    sub.add_parser("seed-prices", help="Insert SPEC default model prices (owner-run, idempotent)")

    eval_parser = sub.add_parser("eval", help="Run the acceptance eval set (offline by default)")
    eval_parser.add_argument(
        "--live", action="store_true", help="real OpenAI, real database, real money"
    )
    eval_parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation of a live run"
    )
    eval_parser.add_argument(
        "--out", type=Path, default=None, help="directory for the .md/.json report"
    )
    eval_parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    eval_parser.add_argument("--cases", type=Path, default=None, help="alternative cases.json")
    eval_parser.add_argument("--keep-db", action="store_true", help="keep the scratch database")

    load_parser = sub.add_parser(
        "load-test", help="5 and 30 parallel chats against a scratch database"
    )
    load_parser.add_argument("--chats", type=_int_list, default=[5, 30], help="e.g. 5,30")
    load_parser.add_argument(
        "--voice-every", type=int, default=5, help="every N-th message is a voice note"
    )
    load_parser.add_argument(
        "--latency", type=float, default=0.5, help="simulated model latency, seconds"
    )
    load_parser.add_argument("--out", type=Path, default=None)
    load_parser.add_argument("--json", action="store_true")
    load_parser.add_argument("--keep-db", action="store_true")

    preflight_parser = sub.add_parser(
        "preflight", help="Readiness checks before switching DRY_RUN off"
    )
    preflight_parser.add_argument(
        "--with-provider",
        action="store_true",
        help="also ask GREEN-API about the instance (paid requests)",
    )
    preflight_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "create-admin":
        raise SystemExit(asyncio.run(_cmd_create_admin()))
    if args.command == "reset-admin-password":
        raise SystemExit(asyncio.run(_cmd_reset_admin_password()))
    if args.command == "kb-import":
        raise SystemExit(asyncio.run(_cmd_kb_import(args.source)))
    if args.command == "seed-prices":
        raise SystemExit(asyncio.run(_cmd_seed_prices()))
    if args.command == "eval":
        from app.eval.runner import CASES_PATH  # noqa: PLC0415

        raise SystemExit(
            asyncio.run(
                _cmd_eval(
                    live=args.live,
                    out=args.out,
                    cases=args.cases or CASES_PATH,
                    keep_db=args.keep_db,
                    yes=args.yes,
                    as_json=args.json,
                )
            )
        )
    if args.command == "load-test":
        raise SystemExit(
            asyncio.run(
                _cmd_load_test(
                    chats=args.chats,
                    voice_every=args.voice_every,
                    latency=args.latency,
                    out=args.out,
                    keep_db=args.keep_db,
                    as_json=args.json,
                )
            )
        )
    if args.command == "preflight":
        raise SystemExit(
            asyncio.run(_cmd_preflight(with_provider=args.with_provider, as_json=args.json))
        )


if __name__ == "__main__":
    main()
