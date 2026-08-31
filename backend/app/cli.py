"""Management commands. Passwords are read from stdin (getpass), never from
argv — command-line arguments end up in shell history."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.config import get_settings
from app.db.engine import build_engine, build_sessionmaker
from app.services.admin_setup import (
    AdminSetupError,
    create_admin,
    reset_admin_password,
)
from app.services.phone import PhoneValidationError


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create the single admin account")
    sub.add_parser("reset-admin-password", help="Reset the admin password")
    args = parser.parse_args()

    if args.command == "create-admin":
        raise SystemExit(asyncio.run(_cmd_create_admin()))
    if args.command == "reset-admin-password":
        raise SystemExit(asyncio.run(_cmd_reset_admin_password()))


if __name__ == "__main__":
    main()
