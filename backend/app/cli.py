"""Management commands. Passwords are read from stdin (getpass), never from
argv — command-line arguments end up in shell history."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from app.config import get_settings
from app.db.engine import build_engine, build_sessionmaker
from app.services.admin_setup import (
    AdminSetupError,
    create_admin,
    reset_admin_password,
)
from app.services.kb_import import import_savvi_export
from app.services.phone import PhoneValidationError
from app.services.pricing import seed_default_prices


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
    args = parser.parse_args()

    if args.command == "create-admin":
        raise SystemExit(asyncio.run(_cmd_create_admin()))
    if args.command == "reset-admin-password":
        raise SystemExit(asyncio.run(_cmd_reset_admin_password()))
    if args.command == "kb-import":
        raise SystemExit(asyncio.run(_cmd_kb_import(args.source)))
    if args.command == "seed-prices":
        raise SystemExit(asyncio.run(_cmd_seed_prices()))


if __name__ == "__main__":
    main()
