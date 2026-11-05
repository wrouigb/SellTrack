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

### Changed
- The original app moved to `legacy/` while it is rebuilt.
- Even splits now total exactly 100%, and all money is exact to the cent.
- Product search and sale search ignore case and treat `%` and `_` as ordinary characters.

### Security
- The app no longer has a fallback secret key, so login cookies can't be forged, and it can't
  start in debug mode outside development.
- Added CSRF protection, rate limits on anything that checks a password or recovery code, secure
  cookies, revocable sessions, a strict Content Security Policy and other security headers.
- Failed logins take the same time and give the same message whether or not the username exists.
- Product names are unique at the database level, not just in Python.
