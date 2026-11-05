# When something's wrong

`stack` is defined in [the deploy runbook](deploy.md#running-a-command-in-a-stack).

## First, look

```bash
stack ps
stack logs --tail=100 web
curl -s http://localhost:8080/readyz
df -h
journalctl -u selltrack-deploy@prod --since today
```

Error pages show people a reference ID. It's the `request_id` in the app's logs, and in the audit log:

```bash
stack logs web | grep <reference>
```

## The site shows "Back in a moment"

Nginx can't reach the app. If it lasts more than a minute or so, it's more than a restart.

```bash
stack ps
stack logs --tail=100 web
```

- **A deploy just happened:** check the journal. It may be going back by itself.
- **The app won't start:** the log says why. `ConfigError` means something in `.env` is missing or weak.
- **Out of memory:** look for `OOMKilled` in `docker inspect`. Each password check takes 64 MiB while it runs. See `mem_limit` in the compose file.

To restart everything:

```bash
stack up --detach --wait --remove-orphans
```

## The database isn't responding

```bash
stack ps db
stack logs --tail=100 db
```

The usual reason is a full disk, below. If the data itself is damaged, [restore from a backup](restore.md).

## Redis isn't responding

The site carries on. Each worker counts rate limits for itself, and dashboards go straight to the database. Restarting it loses nothing that matters:

```bash
stack restart redis
```

## The disk is filling up

```bash
df -h
docker system df
du -sh /opt/selltrack/*/backups
```

- Old images: `docker image prune --all --force --filter until=168h`. Images a stack is using are safe, and any others can be pulled again.
- Backups: the newest 14 are kept. To keep fewer, change `BACKUP_KEEP` in `.env`.
- Logs are limited to 30 MB per container, so they shouldn't be the problem.
- Never delete anything under Docker's `volumes` folder by hand. The database is in there.

## Backups are late

```bash
stack ps backup
stack logs --tail=50 backup
ls -lt /opt/selltrack/prod/backups/ | head
```

- **Permission denied:** `BACKUP_UID` in `.env` has to be the user who owns the backup folder.
- **Password authentication failed:** run `stack run --rm db-roles`, then `stack restart backup`.
- **To take one now:** `stack run --rm backup --once`.

## A deploy keeps failing

```bash
journalctl -u selltrack-deploy@prod | tail -n 50
cat /opt/selltrack/prod/failed-tag
```

A failed release isn't tried again, so nothing more will happen until a different one arrives. Fix it and merge, or [promote an earlier release](rollback.md). The old release carries on in the meantime.

If it fails with `FAILED: going back to ... didn't work either`, the site may be down. Follow "Back in a moment" above.

## You think an account has been broken into

1. Suspend it in the admin console, which ends its sessions straight away.
2. Look at its activity on its page, and in the audit log.
3. Reset its password once you've spoken to the person, then reactivate it.

## You think the server has been broken into

Take it off the network, and assume every secret in `.env` is known. Install again on a clean system with [new secrets](install.md), and restore from a backup taken before it happened. Then tell people. Passwords are hashed with Argon2id, but it's still sensible to ask everyone to change theirs.
