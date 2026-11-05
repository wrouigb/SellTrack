# Deploying and promoting

## How a release reaches the server

1. A pull request has to pass [CI](../../.github/workflows/ci.yml): lint, types, every test, secret and vulnerability scans, a real run of the production stack with a backup and restore, and a build for Raspberry Pi.
2. After merging, CI runs again on `main`. If it passes, [Release](../../.github/workflows/release.yml) builds images for PC and Raspberry Pi, tags them `sha-<commit>`, and moves `:staging` to them.
3. Any stack following `staging` updates itself within two minutes.
4. Production only moves when you promote.

## Promoting to production

On GitHub, go to **Actions**, then **Promote to production**, then **Run workflow** on `main`. Leave the revision empty to promote whatever staging is running. Production updates within two minutes. Nothing is rebuilt: it's the same images staging has been running.

Before promoting, it's worth trying staging for a minute: log in, and add and delete a sale.

## What a deploy does

A timer runs [`pi-deploy.sh`](../../deploy/pi-deploy.sh) every two minutes. When the stack's channel has moved, it:

1. Pulls both images, and takes the compose file and scripts out of the image, so they always match the release.
2. Starts the database and Redis, sets up the roles, runs migrations as the migrator role, and applies the rules for roles again.
3. Replaces the running containers, and waits until they're healthy.
4. Runs the [smoke check](../../deploy/smoke.py): health checks, security headers, assets and their cache headers, then logs in as the smoke account and adds and deletes a sale.
5. Records the release as deployed.

**If step 2 fails,** nothing running has been touched, so the old release carries on.

**If step 3 or 4 fails,** it goes back to the previous release by itself. Either way the release is remembered as failed, and isn't tried again until a different one arrives. A release that works clears that.

A late backup is only a warning in the smoke check, because a new release didn't cause it.

## Watching it

```bash
systemctl list-timers 'selltrack-deploy@*'
journalctl -u selltrack-deploy@prod --since today
cat /opt/selltrack/prod/deployed-tag
cat /opt/selltrack/prod/failed-tag   # only there if the latest release failed
```

Most runs log nothing at all, because nothing has changed. **A failed deploy doesn't notify you.** There's no email, so `systemctl --failed` and the journal are where it shows.

## Running a command in a stack

Everything needs the same options, so this is worth a shell alias:

```bash
stack() {
  sudo -u selltrack IMAGE_TAG="$(cat /opt/selltrack/prod/deployed-tag)" \
    BACKUP_PATH="${BACKUP_PATH:-/opt/selltrack/prod/backups}" \
    docker compose --project-name selltrack-prod --project-directory /opt/selltrack/prod/release \
    --env-file /opt/selltrack/prod/.env --file /opt/selltrack/prod/release/compose.prod.yml "$@"
}

stack ps
stack logs --tail=100 web
```

The rest of these runbooks use `stack`.

## When the deploy script itself changes

The script doesn't update itself, because a bad version could then never deploy its own fix. When a release includes a newer one, the log says so. Copy it over:

```bash
sudo install -o selltrack -m 755 /opt/selltrack/prod/release/pi-deploy.sh /opt/selltrack/pi-deploy.sh
```

## Changing a setting or a password

Change it in the stack's `.env`, then:

```bash
stack run --rm db-roles   # only if a database password changed
stack up --detach --wait --remove-orphans
```

`POSTGRES_PASSWORD` only takes effect when the database is first created. To change it later, use `ALTER ROLE postgres PASSWORD` too. Changing `SECRET_KEY` logs everyone out.
