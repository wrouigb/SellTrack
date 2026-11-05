#!/bin/sh
# Backs up the database each night, keeping the newest 14. Runs in the Postgres image, connecting
# as the read-only role, and reads its connection from the usual PG* settings.
#
#   backup.sh          keep running, as the backup container does
#   backup.sh --once   take one backup now, and stop
#
# If it was down when a backup was due, it takes one as soon as it's back.
set -u

folder="${BACKUP_DIR:-/backups}"
keep="${BACKUP_KEEP:-14}"
hour="${BACKUP_HOUR_UTC:-16}"
retry_seconds=900
day_seconds=86400

# The files hold everything, so nobody else on the machine gets to read them.
umask 077

log() {
    printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

take_backup() {
    stamp=$(date -u +%Y%m%d-%H%M%S)
    # A leading dot, because the system page ignores those. So an unfinished file can't be mistaken
    # for last night's backup.
    partial="$folder/.in-progress-$stamp"
    final="$folder/selltrack-$stamp.dump"

    if ! pg_dump --format=custom --file="$partial"; then
        log "pg_dump failed"
        rm -f -- "$partial"
        return 1
    fi
    # A file that can't be read back isn't a backup.
    if ! pg_restore --list "$partial" >/dev/null; then
        log "the dump can't be read"
        rm -f -- "$partial"
        return 1
    fi
    mv -- "$partial" "$final"
    log "saved $(basename "$final"), $(wc -c <"$final") bytes"

    # Newest first, so everything past the number to keep goes.
    # shellcheck disable=SC2012 # the names are ours, with no spaces or odd characters
    ls -1t "$folder"/selltrack-*.dump | tail -n +"$((keep + 1))" | while read -r old; do
        rm -f -- "$old"
        log "removed $(basename "$old")"
    done
    return 0
}

# Seconds until the next backup is due, or 0 if one is due now.
seconds_until_due() {
    now=$(date -u +%s)
    slot=$((now - now % day_seconds + hour * 3600))
    if [ "$slot" -gt "$now" ]; then
        slot=$((slot - day_seconds))
    fi
    # shellcheck disable=SC2012 # as above
    newest=$(ls -1t "$folder"/selltrack-*.dump 2>/dev/null | head -n 1)
    if [ -z "$newest" ] || [ "$(stat -c %Y "$newest")" -lt "$slot" ]; then
        echo 0
    else
        echo $((slot + day_seconds - now))
    fi
}

if [ "${1:-}" = "--once" ]; then
    take_backup
    exit $?
fi

trap 'log "stopping"; exit 0' TERM INT
log "started, keeping $keep backups, due at $hour:00 UTC"
while :; do
    wait_seconds=$(seconds_until_due)
    if [ "$wait_seconds" -eq 0 ]; then
        take_backup || wait_seconds=$retry_seconds
    fi
    if [ "$wait_seconds" -gt 0 ]; then
        # In the background, so that a signal to stop doesn't have to wait for it.
        sleep "$wait_seconds" &
        wait $!
    fi
done
