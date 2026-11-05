#!/usr/bin/env bash
# Runs the real deploy script with real Docker against a local registry: a first deploy, an upgrade,
# each way a release can fail, and promoting an older release. CI runs this. It needs both images
# already built, as selltrack-ci:ci and selltrack-ci-nginx:ci.
set -Eeuo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
stack=/tmp/pi/prod
registry=localhost:5000/selltrack
port=8082

good_one=$(printf 'a%.0s' {1..40})
good_two=$(printf 'b%.0s' {1..40})
smoke_fails=$(printf 'c%.0s' {1..40})
wont_start=$(printf 'd%.0s' {1..40})
migration_fails=$(printf 'e%.0s' {1..40})

step() { printf '\n== %s\n' "$*"; }

compose() {
    IMAGE_REPO="$registry" IMAGE_TAG="$(cat "$stack/deployed-tag")" BACKUP_PATH="$stack/backups" docker compose \
        --project-name selltrack-prod --project-directory "$stack/release" \
        --env-file "$stack/.env" --file "$stack/release/compose.prod.yml" "$@"
}

show_logs() {
    local status=$? line=$1 command=$2
    trap - ERR
    compose ps --all || true
    compose logs --tail=20 web || true
    echo "FAILED, at line $line: $command" >&2
    exit "$status"
}
trap 'show_logs "$LINENO" "$BASH_COMMAND"' ERR

# A release is the two images under one tag, with its revision in the label. "How" is what's wrong
# with it, as Dockerfile lines added to the app's image.
release() {
    local revision=$1 how=${2:-}
    printf 'FROM selltrack-ci:ci\n%b\n' "$how" |
        docker build --quiet --label "org.opencontainers.image.revision=$revision" \
            --tag "$registry:sha-$revision" - >/dev/null
    docker tag selltrack-ci-nginx:ci "$registry-nginx:sha-$revision"
    docker push --quiet "$registry:sha-$revision" >/dev/null
    docker push --quiet "$registry-nginx:sha-$revision" >/dev/null
    promote "$revision"
}

promote() {
    docker tag "$registry:sha-$1" "$registry:prod"
    docker push --quiet "$registry:prod" >/dev/null
}

deploy() {
    "$root/deploy/pi-deploy.sh" "$stack"
}

running_revision() {
    docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "$(compose ps --quiet web)"
}

check_serving() {
    local revision=$1
    test "$(cat "$stack/deployed-tag")" = "sha-$revision"
    test "$(running_revision)" = "$revision"
    curl --fail --silent --show-error "http://127.0.0.1:$port/readyz"
    echo
}

secret() { openssl rand -hex 32; }

docker run --detach --rm --name selltrack-registry --publish 127.0.0.1:5000:5000 registry:2 >/dev/null
mkdir -p "$stack"
cat >"$stack/.env" <<SETTINGS
IMAGE_REPO=$registry
IMAGE_CHANNEL=prod
APP_ENV=production
SECRET_KEY=$(secret)
TRUSTED_PROXY_COUNT=1
POSTGRES_PASSWORD=$(secret)
DB_MIGRATOR_PASSWORD=$(secret)
DB_APP_PASSWORD=$(secret)
DB_BACKUP_PASSWORD=$(secret)
HTTP_PORT=$port
BACKUP_UID=$(id -u)
SMOKE_USERNAME=smoke-check
SMOKE_PASSWORD=$(secret)
SETTINGS

step "the first deploy"
release "$good_one"
deploy
check_serving "$good_one"

step "nothing new, so nothing happens and nothing is logged"
test -z "$(deploy)"

step "an upgrade"
release "$good_two"
deploy
check_serving "$good_two"
web_before=$(compose ps --quiet web)

step "a release that fails its smoke check goes back by itself"
release "$smoke_fails" 'USER 0\nRUN printf "raise SystemExit(1)\\n" > deploy/smoke.py\nUSER 10001'
if deploy; then
    echo "That should have failed" >&2
    exit 1
fi
test "$(cat "$stack/failed-tag")" = "sha-$smoke_fails"
check_serving "$good_two"

step "and isn't tried again"
deploy | grep 'failed earlier'
check_serving "$good_two"

step "a release that won't start goes back by itself"
release "$wont_start" 'CMD ["sh", "-c", "exit 1"]'
if deploy; then
    echo "That should have failed" >&2
    exit 1
fi
test "$(cat "$stack/failed-tag")" = "sha-$wont_start"
check_serving "$good_two"

step "a failed migration doesn't touch what's running"
web_before=$(compose ps --quiet web)
release "$migration_fails" 'USER 0\nRUN printf "raise SystemExit(1)\\n" > migrations/env.py\nUSER 10001'
if deploy; then
    echo "That should have failed" >&2
    exit 1
fi
test "$(cat "$stack/failed-tag")" = "sha-$migration_fails"
test "$(compose ps --quiet web)" = "$web_before"
check_serving "$good_two"

step "promoting an older release, with a database that's ahead of it"
compose exec --no-TTY db psql --username=postgres --dbname=selltrack --command \
    "UPDATE alembic_version SET version_num = '9999_from_a_later_release'"
promote "$good_one"
deploy
check_serving "$good_one"
test ! -e "$stack/failed-tag"
compose exec --no-TTY db psql --username=postgres --dbname=selltrack --tuples-only --command \
    "SELECT version_num FROM alembic_version" | grep 9999_from_a_later_release

step "stopping"
compose down --volumes --timeout 30
docker rm --force selltrack-registry >/dev/null
echo "All good."
