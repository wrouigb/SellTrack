# Running SellTrack as an admin

The admin console is at `/admin`. You can see accounts and how much each has done, but **not what's in them**: no sales, products, customers, split figures or names of people in a split. The only exception is what someone sends you through support. The privacy page tells people this, so please keep it that way.

Create the first admin on the server:

```bash
stack run --rm web flask users create-admin
```

`stack` is defined in [the deploy runbook](runbooks/deploy.md#running-a-command-in-a-stack).

It asks for a username and password. Admins use the app like anyone else, and reach the console from the account menu.

## Overview and system health

**Overview** shows counts of accounts and anything waiting for a reply, along with the state of the server. **System** has the detail, and says plainly if something needs a look:

| Line | What's wrong looks like | What to do |
|---|---|---|
| Database | Not responding | [When something's wrong](runbooks/incident.md) |
| Redis | Not responding | The app carries on, with rate limits counted separately by each worker and the dashboard a little slower. Restart it |
| Disk | Getting full, at 85% used | [Free up some space](runbooks/incident.md#the-disk-is-filling-up) |
| Last backup | Late, after 26 hours | [Check the backups](runbooks/incident.md#backups-are-late) |
| Last cleanup | Late, after 26 hours | Check the scheduler is running |

## Accounts

Search, filter and sort under **Accounts**. Tick several to suspend them together.

- **Suspending** logs someone out everywhere, and stops them logging in. Nothing is deleted. You can't suspend yourself.
- **Reactivating** lets them log in again. Their old sessions stay ended, in case one was stolen.
- **Resetting a password** is for someone who has lost their password and their recovery codes. You get a temporary password, once, to pass on some other way, and they have to change it when they log in. **Make sure it's really them first.** With no email, you're the only check there is, so ask about something only they'd know. Everything they've entered is in that account.

## Support

Messages wait in **Support**, oldest first. Replying sends the person a notification, and you can change a reply, which tells them again. Close a message when it needs nothing more.

Anyone who can't log in can't use the form, so they'll need another way to reach you.

## Announcements

An announcement goes to the notifications of every active account, including yours, and can't be taken back. A link has to be a page on this site, like `/help`.

## The audit log

A record of security-related actions, which can't be edited, even by the app. Filter by what happened, who did it and when. "No account" means nobody was logged in, such as a failed login, or that the account has since been deleted.

## Feature flags

A flag lets you switch something on for some people first. No feature uses one yet. When one does, it asks `flags.is_enabled("the-name", user_id)`.

- **On** is the master switch. Off means off for everyone, whatever else is set.
- **Rollout** is the share of accounts that get it. Raising it only ever adds people.
- **Always on for** lists usernames that get it whatever the percentage.

## Keeping it running

- [Deploying and promoting](runbooks/deploy.md)
- [Rolling back](runbooks/rollback.md)
- [Restoring from a backup](runbooks/restore.md)
- [When something's wrong](runbooks/incident.md)
