#!/bin/sh
set -eu

archive="${1:-}"
if [ ! -f "$archive" ]; then
    echo "Backup archive does not exist" >&2
    exit 1
fi

if [ -f /run/secrets/postgres_password ]; then
    export PGPASSWORD="$(tr -d '\r\n' < /run/secrets/postgres_password)"
elif [ -z "${PGPASSWORD:-}" ]; then
    echo "PostgreSQL password is unavailable" >&2
    exit 1
fi
restore_database="neurokolo_restore_check_$(date -u +%Y%m%d%H%M%S)_$$"

cleanup() {
    dropdb --if-exists "$restore_database" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

createdb "$restore_database"
pg_restore \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    --dbname="$restore_database" \
    "$archive"

migration_count="$(
    psql --dbname="$restore_database" --tuples-only --no-align --command \
        "SELECT count(*) FROM alembic_version"
)"
if [ "$migration_count" -ne 1 ]; then
    echo "Restored database does not contain exactly one Alembic revision" >&2
    exit 1
fi

psql --dbname="$restore_database" --tuples-only --no-align --command \
    "SELECT count(*) FROM plans" >/dev/null
