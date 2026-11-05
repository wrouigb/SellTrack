# Restoring from a backup

**Restoring loses everything since the backup was taken,** so be sure it's what you want. If only one person's data is wrong, talk to them first: the whole site goes back.

CI follows these same steps on every pull request, with real images (`tests/stack_check.sh`), and a test does the same on real files (`tests/integration/test_db_roles.py`).

`stack` is defined in [the deploy runbook](deploy.md#running-a-command-in-a-stack). These steps are for production. Use the right folder if it's another stack.

## 1. Stop the deploys, and anything using the database

```bash
sudo systemctl stop selltrack-deploy@prod.timer
stack stop web scheduler backup
```

Nginx stays up, and shows people a "back in a moment" page.

## 2. Choose a backup

```bash
ls -lt /opt/selltrack/prod/backups/
```

There's one a night, and the newest 14 are kept. **Copy the one you choose somewhere safe first,** and take one of the database as it is now, in case you want to come back to it:

```bash
stack run --rm backup --once
```

(That only works if the database still runs. If it doesn't, carry on.)

## 3. Replace the database

```bash
stack exec db psql --username=postgres --dbname=postgres \
  --command='DROP DATABASE selltrack WITH (FORCE)'
stack exec db psql --username=postgres --dbname=postgres \
  --command='CREATE DATABASE selltrack'
stack exec --no-TTY db pg_restore --username=postgres --dbname=selltrack --exit-on-error \
  < /opt/selltrack/prod/backups/selltrack-YYYYMMDD-HHMMSS.dump
```

If the whole database volume was lost, `stack up --detach --wait db` creates a new one first. That also needs `stack run --rm db-roles` before the `pg_restore`, because the backup refers to those roles.

## 4. Put the roles back, and start again

```bash
stack run --rm db-roles
stack run --rm migrate      # only does anything if the backup is older than this release
stack run --rm db-roles
stack up --detach --wait --remove-orphans
stack run --rm smoke
```

The smoke check should finish with `All good.`

## 5. Start the deploys again

```bash
sudo systemctl start selltrack-deploy@prod.timer
```

## 6. Afterwards

- Log in and look at the dashboard, and the audit log for the time you restored to.
- Consider [telling everyone](../admin-guide.md#announcements) that anything entered after the backup was taken has gone. They'll need to enter it again.
- Sessions from after the backup may have ended, so people may need to log in again.

## If you've lost the whole machine

Backups only help if they weren't on it, which is why they should be copied elsewhere regularly. Follow [the install runbook](install.md) with the same `.env` if you have it, or new passwords if you don't: the roles are set up again from `.env` either way. A new `SECRET_KEY` just logs everyone out. Then follow these steps from step 3.
