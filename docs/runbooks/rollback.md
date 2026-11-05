# Rolling back

A failed deploy already goes back by itself, so you only need this when a release passes its checks but turns out to be wrong.

## The normal way: promote the earlier release

On GitHub, go to **Actions**, then **Promote to production**, then **Run workflow** on `main`, with the full commit of the release to go back to as the revision. Production moves to it within two minutes.

To find that commit, look at `main`'s history, or the previous tag in the journal:

```bash
journalctl -u selltrack-deploy@prod | grep deploying
```

This is the way to choose if you can, because it also stops the bad release being deployed again.

Migrations always leave the schema working for the release before, so the older release runs on the newer database. Its deploy sees that the database is ahead of it, and leaves it alone.

## Right now, by hand

If GitHub can't be reached, or it can't wait:

```bash
sudo systemctl stop selltrack-deploy@prod.timer   # or the next run undoes this
sudo -u selltrack IMAGE_TAG=sha-<earlier commit> BACKUP_PATH=/opt/selltrack/prod/backups \
  docker compose --project-name selltrack-prod --project-directory /opt/selltrack/prod/release \
  --env-file /opt/selltrack/prod/.env --file /opt/selltrack/prod/release/compose.prod.yml \
  up --detach --wait --remove-orphans
```

Once it's fixed properly, promote a good release and start the timer again:

```bash
sudo systemctl start selltrack-deploy@prod.timer
```

## If the data is wrong

Going back to an older release doesn't undo what the newer one wrote. If data has been damaged, [restore from a backup](restore.md), which loses anything since that backup.
