# Changelog

All notable changes to SellTrack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Exact money handling in Australian dollars (`app/money.py`). Amounts are `Decimal`, never
  floats; input like `NaN`, `Infinity`, `1e3` or non-ASCII digits is rejected; even splits always
  total exactly 100%.
- Profit split engine (`app/services/split_engine.py`): people with % shares, upfront costs paid
  back to whoever paid them, sale money tracked by who received it, part-payments, and suggested
  settle-up transfers that bring every balance to $0.
- Unit tests, Ruff, mypy (strict) and pytest configuration in `pyproject.toml`.
- The approved rebuild plan in `docs/plan.md`.
- App factory and `Development`/`Testing`/`Staging`/`Production` configuration.
- PostgreSQL schema and first migration, with database checks that reject bad money (including
  `NaN`), and foreign keys that keep each row inside its own account.
- Accounts: sign-up, login, logout, one-time recovery codes, change password, log out everywhere,
  recent sign-ins, Argon2id hashing and a bundled common-password blocklist.
- Admin accounts created with `flask users create-admin`, permission checks, and services to
  suspend an account or reset its password.
- Services for products and cost history, sales (filters, sorting, paging, delete with undo),
  dashboard reporting, and the profit split on real data (people, upfront costs, payments, settle
  up, undo).
- In-app notifications, feature flags (on/off, percentage rollout, allowlist) and an audit log.
- `/healthz` and `/readyz`, JSON logs with request IDs, friendly error pages and optional Sentry.
- Integration tests on real Postgres and Redis.

- The designed pages: dashboard, sales, products, the five split tabs, settings, onboarding,
  landing, help, privacy, terms and error pages, in light and dark, usable from 360px wide and
  with JavaScript turned off.
- The dashboard: period selector, figures with their change, a chart of money in and profit, top
  products, latest sales, who owes whom, and next steps.
- Sales list: filters, sorting, paging, bulk delete with undo, and CSV download. Spreadsheet
  formulas in customer names and notes are defused in the file.

- The admin console at `/admin`: accounts with search, filters, sorting, paging and bulk suspend;
  account details and activity counts; password resets; a support inbox; announcements; the audit
  log; feature flags; and system health, including disk space and the last backup.
- Notifications: a page, and a bell that keeps its count up to date by itself.
- Support: a form on the help page and a page of your own messages and their replies.
- Download everything in your account as a zip of CSV files.
- A nightly cleanup (`flask jobs scheduler`), which removes sales deleted over 30 days ago, old
  sign-ins and old read notifications.
- Dashboard totals are cached in Redis, and stop being used as soon as anything changes them.
- `app/services/entitlements.py`, the single place billing limits would go. Everything is unlimited.
- Docker images for PC and Raspberry Pi, compose files for local use and for production, Nginx and
  Gunicorn settings, with containers that run as non-root on read-only file systems.
- Nightly backups that are checked before being kept, with the newest 14 kept, and a restore that
  CI tests on every pull request.
- Three database roles, so that the app can't change the schema or alter the audit log, and
  backups can't change anything.
- CI, release and promote workflows, and a deploy script that goes back to the previous release by
  itself if a smoke check fails.
- A Makefile, pre-commit hooks, Dependabot, schema and endpoint references that tests keep accurate,
  guides for users and admins, and runbooks for installing, deploying, rolling back, restoring and
  incidents.

### Changed
- A sale's price fills in when its product is already chosen, such as on the first sale in
  onboarding, as well as when you pick one.
- CSV downloads open correctly in Excel, including names with accents.
- Public pages offer a way back to the app, rather than "Log in", when you're already logged in.
- The original app has been replaced. Its old addresses redirect to the new ones.
- Even splits now total exactly 100%, and all money is exact to the cent.
- Product search and sale search ignore case and treat `%` and `_` as ordinary characters.

### Security
- The app no longer has a fallback secret key, so login cookies can't be forged, and it can't
  start in debug mode outside development.
- Added CSRF protection, rate limits on anything that checks a password or recovery code, secure
  cookies, revocable sessions, a strict Content Security Policy and other security headers.
- Failed logins take the same time and give the same message whether or not the username exists.
- Product names are unique at the database level, not just in Python.
- Confirmations use a native dialog with the message set as text, so a product name can't inject
  script, which the original's inline `confirm()` allowed.
- Pages aren't kept in browser storage, so a shared computer can't show them later.
- Suspending an account ends its sessions for good, so reactivating it can't bring back one that
  was stolen.
- The audit log no longer keeps settle-up figures or the names of people in a split. It outlives
  the account, so deleting an account now really does remove them.
- Cached figures are stored as JSON rather than pickle, so getting into Redis can't run code.
