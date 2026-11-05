#!/usr/bin/env bash
# The last part of the README's quick start, `docker compose up`, in a form CI can check.
set -Eeuo pipefail

show_logs() {
    docker compose ps --all
    docker compose logs --tail=40
}
trap show_logs ERR

docker compose up --build --detach
for _ in $(seq 1 60); do
    if curl --fail --silent --output /dev/null http://localhost:8080/readyz; then
        break
    fi
    sleep 2
done
curl --fail --silent --show-error http://localhost:8080/readyz
echo
curl --fail --silent --show-error --output /dev/null http://localhost:8080/
curl --fail --silent --show-error --output /dev/null http://localhost:8080/login
docker compose down
echo "All good."
