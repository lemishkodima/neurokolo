#!/bin/sh
set -eu

umask 077

retention_days="${BACKUP_RETENTION_DAYS:-14}"
interval_seconds="${BACKUP_INTERVAL_SECONDS:-86400}"
case "$retention_days:$interval_seconds" in
    *[!0-9:]* | :* | *:) echo "Backup timing values must be positive integers" >&2; exit 1 ;;
esac

if [ -f /run/secrets/postgres_password ]; then
    export PGPASSWORD="$(tr -d '\r\n' < /run/secrets/postgres_password)"
elif [ -z "${PGPASSWORD:-}" ]; then
    echo "PostgreSQL password is unavailable" >&2
    exit 1
fi

while true; do
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="/backups/neurokolo-${timestamp}.dump"
    temporary="/backups/.neurokolo-${timestamp}.tmp"

    pg_dump \
        --format=custom \
        --compress=9 \
        --no-owner \
        --no-privileges \
        --file="$temporary"
    pg_restore --list "$temporary" >/dev/null
    mv "$temporary" "$target"

    if [ "${VERIFY_RESTORE:-true}" = "true" ]; then
        /bin/sh /ops/verify-backup.sh "$target"
    fi

    touch /backups/.last_success
    find /backups -type f -name 'neurokolo-*.dump' -mtime "+$retention_days" -delete
    echo "Backup and restore verification completed at ${timestamp}"

    if [ "${RUN_ONCE:-false}" = "true" ]; then
        exit 0
    fi
    sleep "$interval_seconds"
done
