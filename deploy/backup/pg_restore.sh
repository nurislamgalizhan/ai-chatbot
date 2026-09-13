#!/usr/bin/env bash
# Restore a dump into the RUNNING postgres container, with the
# application stopped. Destructive on purpose: it drops and recreates
# every object the dump contains, so it asks out loud before doing it.
#
#   ./deploy/backup/pg_restore.sh /var/backups/wabot/wabot-2026-09-14.dump
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

COMPOSE="${COMPOSE:-docker compose}"
DUMP="${1:-}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[ -n "$DUMP" ] || die "usage: $0 /var/backups/wabot/wabot-YYYY-MM-DD.dump"
[ -f "$DUMP" ] || die "no such dump: $DUMP"
head -c 5 "$DUMP" | grep -q PGDMP || die "not a pg_dump -Fc file: $DUMP"
[ -f .env ] || die ".env not found in $REPO_DIR"

POSTGRES_DB="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
[ -n "$POSTGRES_DB" ] || die "POSTGRES_DB is empty in .env"

cat <<WARN

  ВНИМАНИЕ. Восстановление ЗАМЕНИТ текущие данные базы "$POSTGRES_DB"
  содержимым файла:

      $DUMP

  Всё, что появилось после этого дампа (переписка, расходы, настройки),
  будет потеряно. Перед восстановлением стоит сделать свежий дамп:
  ./deploy/backup/pg_backup.sh

WARN
printf 'Введите RESTORE чтобы продолжить: '
read -r answer
[ "$answer" = "RESTORE" ] || die "отменено (введено: '$answer')"

log "stopping application containers (postgres stays up)"
$COMPOSE stop backend worker frontend

# Whatever happens below — an error, Ctrl-C, a closed terminal — the
# application must not be left switched off silently.
restart_app() {
    log "bringing the application back up"
    $COMPOSE up -d backend worker frontend >/dev/null 2>&1 || \
        log "WARNING: could not restart the application, do it manually: make up"
}
trap restart_app EXIT

# Restore onto an EMPTY schema, not over the live one. `--clean` only
# drops what the dump itself contains: tables added by migrations newer
# than the dump would survive, leaving a schema that matches no version
# at all while alembic_version says otherwise.
log "dropping the current schema"
$COMPOSE exec -T postgres sh -c \
    'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -c "DROP SCHEMA public CASCADE" -c "CREATE SCHEMA public"'

log "restoring $DUMP"
# --exit-on-error would abort on the first object, so failures are
# counted instead and reported at the end.
if $COMPOSE exec -T postgres sh -c \
    'pg_restore --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$DUMP"; then
    log "restore finished without errors"
else
    log "pg_restore reported errors (see output above) — check the data before starting the app"
fi

log "done. Проверьте: make smoke, затем страницу «Система» в админке."
