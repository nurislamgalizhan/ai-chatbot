#!/usr/bin/env bash
# Daily database dump, kept ON THE SERVER only (owner decision 2026-08-31:
# no offsite copies — the risk is recorded in docs/deployment.md §5).
#
# Run from the repository directory, as a user who may talk to docker:
#   ./deploy/backup/pg_backup.sh
# Cron (root), daily at 03:30:
#   30 3 * * * cd /srv/wabot && ./deploy/backup/pg_backup.sh >> /var/log/wabot-backup.log 2>&1
#
# The dump is written by `docker compose exec` into the container's stdout
# and lands on the host: the database port is never published, and the
# password never appears in the process list (it is read inside the
# container from its own environment).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

BACKUP_DIR="${WABOT_BACKUP_DIR:-/var/backups/wabot}"
COMPOSE="${COMPOSE:-docker compose}"
# Keep: 7 daily, 4 weekly (Monday), 3 monthly (1st of the month).
KEEP_DAILY="${WABOT_KEEP_DAILY:-7}"
KEEP_WEEKLY="${WABOT_KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${WABOT_KEEP_MONTHLY:-3}"
# Refuse to start a dump that could fill the disk of a shared VPS.
MIN_FREE_MB="${WABOT_MIN_FREE_MB:-2048}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

command -v docker >/dev/null || die "docker not found"
[ -f .env ] || die ".env not found in $REPO_DIR"

# Only the database NAME is needed on the host; the user and password
# stay inside the container. Values may be quoted in .env, and a stray
# quote must not end up inside an SQL statement further down.
env_value() {
    local raw
    raw="$(grep -E "^$1=" .env | head -n1 | cut -d= -f2-)"
    raw="${raw%\"}"; raw="${raw#\"}"
    raw="${raw%\'}"; raw="${raw#\'}"
    printf '%s' "$raw"
}

POSTGRES_DB="$(env_value POSTGRES_DB)"
POSTGRES_USER="$(env_value POSTGRES_USER)"
[ -n "$POSTGRES_DB" ] || die "POSTGRES_DB is empty in .env"
[ -n "$POSTGRES_USER" ] || die "POSTGRES_USER is empty in .env"
case "$POSTGRES_DB$POSTGRES_USER" in
    *[\'\"\;]*) die "POSTGRES_DB/POSTGRES_USER contain quotes or semicolons — refusing to build SQL" ;;
esac

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp="$(date -u +%Y-%m-%d)"
target="$BACKUP_DIR/wabot-$stamp.dump"
tmp="$target.part"

# Rotation first: when the disk is nearly full, the old dumps ARE the
# space this dump needs. Doing it the other way round meant the script
# died on "not enough space" and never cleaned anything — a situation
# that never healed by itself.
# --- rotation ------------------------------------------------------------
# Dailies are dropped after KEEP_DAILY days unless they are also a weekly
# (Monday) or monthly (1st) copy that is still inside its own window.
keep_file() {
    local file="$1" date_part day_of_week day_of_month age_days
    date_part="$(basename "$file" | sed -E 's/^wabot-([0-9]{4}-[0-9]{2}-[0-9]{2})\.dump$/\1/')"
    [ "$date_part" != "$(basename "$file")" ] || return 1

    if date -u -d "$date_part" +%s >/dev/null 2>&1; then      # GNU date
        file_epoch="$(date -u -d "$date_part" +%s)"
        day_of_week="$(date -u -d "$date_part" +%u)"
    else                                                       # BSD/macOS date
        file_epoch="$(date -u -j -f %Y-%m-%d "$date_part" +%s)"
        day_of_week="$(date -u -j -f %Y-%m-%d "$date_part" +%u)"
    fi
    day_of_month="${date_part##*-}"
    age_days=$(( ( $(date -u +%s) - file_epoch ) / 86400 ))

    [ "$age_days" -lt "$KEEP_DAILY" ] && return 0
    [ "$day_of_week" = "1" ] && [ "$age_days" -lt $(( KEEP_WEEKLY * 7 )) ] && return 0
    [ "$day_of_month" = "01" ] && [ "$age_days" -lt $(( KEEP_MONTHLY * 31 )) ] && return 0
    return 1
}

rotate() {
    local file
    for file in "$BACKUP_DIR"/wabot-*.dump; do
        [ -e "$file" ] || continue
        # keep_file returns non-zero both for "too old" and for "name I
        # do not recognise". Only the first is a reason to delete: a file
        # someone else put here is not ours to remove.
        case "$(basename "$file")" in
            wabot-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].dump) ;;
            *) continue ;;
        esac
        if keep_file "$file"; then
            continue
        fi
        log "rotating out $(basename "$file")"
        rm -f "$file"
    done
}


rotate

free_mb="$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
[ "$free_mb" -ge "$MIN_FREE_MB" ] || die "only ${free_mb}MB free in $BACKUP_DIR, need ${MIN_FREE_MB}MB"

log "dumping $POSTGRES_DB -> $target"
# -Fc: custom format, restorable selectively by pg_restore.
# Written to a .part file first: an interrupted dump must never look
# like a good backup.
if ! $COMPOSE exec -T postgres sh -c \
    'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$tmp"; then
    rm -f "$tmp"
    die "pg_dump failed"
fi
[ -s "$tmp" ] || { rm -f "$tmp"; die "pg_dump produced an empty file"; }

# A custom-format dump starts with the magic string "PGDMP".
head -c 5 "$tmp" | grep -q PGDMP || { rm -f "$tmp"; die "dump does not look like pg_dump -Fc output"; }

mv "$tmp" "$target"
chmod 600 "$target"
log "dump ready: $(du -h "$target" | cut -f1)"

rotate

kept="$(ls -1 "$BACKUP_DIR"/wabot-*.dump 2>/dev/null | wc -l | tr -d ' ')"
size_bytes="$(wc -c < "$target" | tr -d ' ')"

# Leave the trace INSIDE the database, not only in this log file: the
# admin panel runs read-only with no access to the host filesystem, so
# system_events is the only way «Система» can say when the last backup
# happened — and the only way the worker can notice that backups quietly
# stopped and alert the owner.
# The file name is passed as a psql VARIABLE and quoted by psql itself,
# so nothing from the filesystem can be interpreted as SQL.
$COMPOSE exec -T postgres psql -v ON_ERROR_STOP=1 -q \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v "dumpfile=$(basename "$target")" -v "bytes=$size_bytes" -v "kept=$kept" \
    -c "INSERT INTO system_events (level, component, event, detail) VALUES ('info', 'backup', 'backup_completed', jsonb_build_object('file', :'dumpfile', 'size_bytes', :bytes::bigint, 'kept', :kept::int));" \
    || log "WARNING: dump is fine, but recording it in the database failed"

log "done; $kept dumps kept"
