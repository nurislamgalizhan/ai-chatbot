#!/usr/bin/env bash
# Prove a dump can actually be restored — the only thing that makes a
# backup a backup. Restores the newest dump into a THROWAWAY database
# inside the same postgres container and counts what came back.
#
#   ./deploy/backup/backup_verify.sh [/path/to/dump]
#
# The live database is never touched: the copy is created and dropped by
# this script, and the application keeps running throughout.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

COMPOSE="${COMPOSE:-docker compose}"
BACKUP_DIR="${WABOT_BACKUP_DIR:-/var/backups/wabot}"
CHECK_DB="${WABOT_VERIFY_DB:-wabot_restore_check}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    DUMP="$(ls -1t "$BACKUP_DIR"/wabot-*.dump 2>/dev/null | head -n1 || true)"
fi
[ -n "$DUMP" ] && [ -f "$DUMP" ] || die "no dump found (looked in $BACKUP_DIR)"
head -c 5 "$DUMP" | grep -q PGDMP || die "not a pg_dump -Fc file: $DUMP"

psql_admin() { $COMPOSE exec -T postgres sh -c "psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d postgres $*"; }
psql_check() { $COMPOSE exec -T postgres sh -c "psql -t -A -U \"\$POSTGRES_USER\" -d $CHECK_DB $*"; }

log "verifying $(basename "$DUMP") in a temporary database ($CHECK_DB)"
psql_admin "-c 'DROP DATABASE IF EXISTS $CHECK_DB'"
psql_admin "-c 'CREATE DATABASE $CHECK_DB'"

cleanup() { psql_admin "-c 'DROP DATABASE IF EXISTS $CHECK_DB'" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if ! $COMPOSE exec -T postgres sh -c \
    "pg_restore --no-owner --no-privileges -U \"\$POSTGRES_USER\" -d $CHECK_DB" < "$DUMP"; then
    die "pg_restore failed — this dump is NOT restorable"
fi

# Not just "tables exist": the schema version and the rows that matter.
version="$(psql_check "-c 'SELECT version_num FROM alembic_version'" | tr -d '[:space:]')"
[ -n "$version" ] || die "restored database has no alembic_version — dump is incomplete"

log "schema version: $version"
for table in chats messages ai_calls kb_documents kb_faq settings admin_user; do
    count="$(psql_check "-c 'SELECT count(*) FROM $table'" | tr -d '[:space:]')"
    log "  $table: $count"
done

# pgvector survives the dump only if the extension is present AND the
# binary vector format restored; an unusable vector column would make the
# knowledge base silently unsearchable after a real restore.
vectors="$(psql_check "-c 'SELECT count(*) FROM kb_chunks WHERE embedding IS NOT NULL'" | tr -d '[:space:]')"
log "  kb_chunks with embeddings: $vectors"

log "OK: dump restores cleanly"
