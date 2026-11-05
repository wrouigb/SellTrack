#!/usr/bin/env bash
# Deploys one stack, if its channel has moved to a new release. A systemd timer runs this every two
# minutes. See docs/runbooks/deploy.md.
#
#   pi-deploy.sh /opt/selltrack/prod
#
# The folder holds the stack's .env. Everything else in it is written here:
#   release/, release.previous/   compose file and scripts, taken from the image they came with
#   deployed-tag                  what's running
#   failed-tag                    a release that failed, which won't be tried again
#   backups/                      unless BACKUP_PATH in .env says otherwise
#
# Pull-based, so the machine needs no open ports and no CI credentials, only read access to the
# container registry.
set -Eeuo pipefail

stack_dir=$(cd "${1:?usage: pi-deploy.sh /path/to/stack}" && pwd)
stack_name=$(basename "$stack_dir")
cd "$stack_dir"

wait_seconds=180
release_files=(compose.prod.yml db-roles.sql backup.sh pi-deploy.sh)

log() {
    printf '[%s] %s\n' "$stack_name" "$*"
}

setting() {
    sed -n "s/^$1=//p" .env | tail -n 1
}

read_tag() {
    if [[ -f $1 ]]; then cat "$1"; fi
}

compose() {
    local tag=$1
    shift
    IMAGE_TAG="$tag" BACKUP_PATH="$backup_path" docker compose \
        --project-name "selltrack-$stack_name" \
        --project-directory "$stack_dir/release" \
        --env-file "$stack_dir/.env" \
        --file "$stack_dir/release/compose.prod.yml" \
        "$@"
}

fetch_release() {
    local image=$1 file
    rm -rf release.new
    mkdir release.new
    for file in "${release_files[@]}"; do
        docker run --rm --network none --entrypoint cat "$image" "deploy/$file" >"release.new/$file"
    done
}

use_new_release() {
    rm -rf release.previous
    if [[ -d release ]]; then mv release release.previous; fi
    mv release.new release
}

use_previous_release() {
    if [[ -d release.previous ]]; then
        rm -rf release
        mv release.previous release
    fi
}

update_database() {
    local tag=$1
    compose "$tag" up --detach --wait --wait-timeout "$wait_seconds" db redis &&
        compose "$tag" run --rm db-roles &&
        compose "$tag" run --rm migrate &&
        compose "$tag" run --rm db-roles
}

# Migrations always leave the schema working for the previous release, so it can simply come back.
roll_back() {
    local failed=$1 previous=$2
    echo "$failed" >failed-tag
    if [[ -z $previous ]]; then
        log "FAILED: $failed didn't work, and there's nothing earlier to go back to"
        exit 1
    fi
    log "FAILED: $failed didn't work, so going back to $previous"
    use_previous_release
    if compose "$previous" up --detach --wait --wait-timeout "$wait_seconds" --remove-orphans; then
        log "back on $previous"
    else
        log "FAILED: going back to $previous didn't work either. See docs/runbooks/incident.md"
    fi
    exit 1
}

repo=$(setting IMAGE_REPO)
channel=$(setting IMAGE_CHANNEL)
if [[ -z $repo || -z $channel ]]; then
    log "FAILED: IMAGE_REPO and IMAGE_CHANNEL must be set in $stack_dir/.env"
    exit 1
fi
backup_path=$(setting BACKUP_PATH)
backup_path=${backup_path:-$stack_dir/backups}

# The timer can't start a second copy, but someone running this by hand could.
exec 9>.deploy.lock
if ! flock --nonblock 9; then
    log "another deploy is still running"
    exit 0
fi

docker pull --quiet "$repo:$channel" >/dev/null
revision=$(docker image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$repo:$channel")
if [[ ! $revision =~ ^[0-9a-f]{40}$ ]]; then
    log "FAILED: $repo:$channel has no usable revision label: '$revision'"
    exit 1
fi

target="sha-$revision"
current=$(read_tag deployed-tag)
if [[ $target == "$current" ]]; then
    exit 0
fi
# Otherwise a bad release would be tried, and rolled back, every two minutes. Promoting a different
# release clears this, as a successful deploy removes the file.
if [[ $target == "$(read_tag failed-tag)" ]]; then
    log "$target failed earlier, so waiting for a different release"
    exit 0
fi

log "deploying $target, replacing ${current:-nothing}"
docker pull --quiet "$repo:$target" >/dev/null
docker pull --quiet "$repo-nginx:$target" >/dev/null
mkdir -p "$backup_path"
chmod 755 "$backup_path"
fetch_release "$repo:$target"
use_new_release

# Nothing running has been touched yet, so if this fails the old release just carries on.
if ! update_database "$target"; then
    log "FAILED: couldn't update the database for $target, so ${current:-nothing} carries on"
    echo "$target" >failed-tag
    use_previous_release
    exit 1
fi

if ! compose "$target" up --detach --wait --wait-timeout "$wait_seconds" --remove-orphans; then
    roll_back "$target" "$current"
fi
if ! compose "$target" run --rm smoke; then
    roll_back "$target" "$current"
fi

echo "$target" >deployed-tag
rm -f failed-tag
log "now running $target"

if ! cmp --silent release/pi-deploy.sh "${BASH_SOURCE[0]}"; then
    log "this release has a newer pi-deploy.sh. See docs/runbooks/deploy.md"
fi

# Keeps the disk from filling up. Anything older that's needed again can be pulled again.
docker image prune --all --force --filter until=336h >/dev/null || true
