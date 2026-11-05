#!/usr/bin/env bash
# Runs the production compose file for real, the way pi-deploy.sh does, then checks that backups
# restore and that the database rules hold. CI runs this. It needs both images already built:
#   IMAGE_REPO=selltrack-ci IMAGE_TAG=ci tests/stack_check.sh
set -Eeuo pipefail

stack=/tmp/stack
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$stack/backups"
chmod 755 "$stack/backups"

secret() { openssl rand -hex 32; }
cat >"$stack/.env" <<SETTINGS
IMAGE_REPO=$IMAGE_REPO
IMAGE_TAG=$IMAGE_TAG
APP_ENV=production
SECRET_KEY=$(secret)
TRUSTED_PROXY_COUNT=1
POSTGRES_PASSWORD=$(secret)
DB_MIGRATOR_PASSWORD=$(secret)
DB_APP_PASSWORD=$(secret)
DB_BACKUP_PASSWORD=$(secret)
HTTP_PORT=8081
BACKUP_PATH=$stack/backups
BACKUP_UID=$(id -u)
SMOKE_USERNAME=smoke-check
SMOKE_PASSWORD=$(secret)
SETTINGS

# The compose file's relative paths are next to it, as they are in a release folder.
compose() {
    docker compose --project-directory "$root/deploy" --project-name selltrack-ci \
        --env-file "$stack/.env" --file "$root/deploy/compose.prod.yml" "$@"
}
as_superuser() {
    compose exec --no-TTY db psql --username=postgres --dbname="${1:-selltrack}" \
        --tuples-only --no-align --set=ON_ERROR_STOP=1
}
step() { printf '\n== %s\n' "$*"; }
show_logs() {
    local status=$? line=$1 command=$2
    trap - ERR
    compose logs --tail=15 || true
    compose ps --all || true
    echo "FAILED, at line $line: $command" >&2
    exit "$status"
}
trap 'show_logs "$LINENO" "$BASH_COMMAND"' ERR

step "database, roles and migrations, in the order a deploy does them"
compose up --detach --wait db redis
compose run --rm db-roles
compose run --rm migrate
compose run --rm db-roles

step "everything else"
compose up --detach --wait --remove-orphans

step "the check that decides whether a deploy stays"
compose run --rm smoke

step "through Nginx from outside"
base=http://127.0.0.1:8081
curl --fail --silent --show-error "$base/readyz"
echo
asset=$(curl --fail --silent --show-error "$base/" | grep -o '/static/dist/[^"]*\.css' | head -n 1)
# Nginx doesn't compress a HEAD request, so these need to be real ones.
headers=$(curl --fail --silent --show-error --dump-header - --output /dev/null \
    --header 'Accept-Encoding: gzip' "$base$asset")
echo "$headers" | grep -i '^cache-control: .*immutable'
echo "$headers" | grep -i '^content-encoding: gzip'
curl --silent --output /dev/null --write-out '%{http_code}' "$base/static/src/app.js" | grep -q '^404$'

step "the audit log can only be added to, by the app's own role"
app_sql() {
    compose exec --no-TTY --env "PGPASSWORD=$(sed -n 's/^DB_APP_PASSWORD=//p' "$stack/.env")" db \
        psql --host=localhost --username=selltrack_app --dbname=selltrack --command="$1"
}
app_sql 'SELECT count(*) FROM audit_logs'
if app_sql 'DELETE FROM audit_logs' 2>/dev/null; then
    echo "The app's role was able to delete from the audit log" >&2
    exit 1
fi
if app_sql 'CREATE TABLE should_not_work (id int)' 2>/dev/null; then
    echo "The app's role was able to create a table" >&2
    exit 1
fi

step "a backup"
compose run --rm backup --once
dump=$(find "$stack/backups" -name 'selltrack-*.dump' | sort | tail -n 1)
test -s "$dump"
test "$(stat -c %a "$dump")" = 600

step "restoring it, following docs/runbooks/restore.md"
before=$(echo 'SELECT count(*) FROM users' | as_superuser)
test "$before" -ge 1
compose stop web scheduler backup
echo 'DROP DATABASE selltrack WITH (FORCE)' | as_superuser postgres
echo 'CREATE DATABASE selltrack' | as_superuser postgres
compose exec --no-TTY db pg_restore --username=postgres --dbname=selltrack --exit-on-error <"$dump"
compose run --rm db-roles
compose up --detach --wait --remove-orphans
after=$(echo 'SELECT count(*) FROM users' | as_superuser)
test "$before" = "$after"
compose run --rm smoke
if app_sql 'DELETE FROM audit_logs' 2>/dev/null; then
    echo "The audit log could be changed after a restore" >&2
    exit 1
fi

step "stopping cleanly"
compose down --timeout 30
echo "All good."
