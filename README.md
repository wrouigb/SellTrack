# SellTrack

Track what you sell, see what you make, and split the profit fairly.

> **Status: being rebuilt for production.** The backend, the pages and the admin console are done
> (phases 3 to 5 of the plan in [`docs/plan.md`](docs/plan.md)). Docker and deployment come next.

## What's here

| Path | What it is |
|---|---|
| `app/__init__.py`, `app/config.py` | App factory and configuration. Staging and production refuse to start without a strong secret, Postgres and debug off |
| `app/models/` | The database schema (plan section 7) |
| `migrations/` | Alembic migrations |
| `app/money.py` | Exact AUD money: strict parsing, whole cents, en-AU formatting, totals that never lose a cent |
| `app/services/` | The business rules: accounts, products, sales, reporting, the profit split, notifications, feature flags, audit log |
| `app/security/` | Sessions, passwords (Argon2id), permissions, security headers, rate limits |
| `app/blueprints/`, `app/forms/` | Thin routes and forms |
| `app/templates/` | Pages, built from the components in `app/templates/components/` |
| `app/static/src/` | The design tokens and styles (`app.css`) and the small amount of JavaScript (`app.js`) |
| `app/charts.py` | The geometry of the dashboard's chart, drawn on the server as SVG |
| `tests/` | Unit tests, integration tests on real Postgres and Redis, and browser tests |

## Setting up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, PostgreSQL 16 and Redis.

```bash
uv sync
npm ci
npm run build
cp .env.example .env

# As a Postgres superuser. The role only needs to own its databases, not be a superuser.
psql -c "CREATE ROLE selltrack LOGIN PASSWORD 'selltrack'"
psql -c "CREATE DATABASE selltrack_dev OWNER selltrack"
psql -c "CREATE DATABASE selltrack_test OWNER selltrack"

export FLASK_APP=app:create_app
uv run flask db upgrade
uv run flask users create-admin
uv run flask run --debug
```

While changing styles or JavaScript, `npm run watch` rebuilds them as you save. The app picks up a
rebuild by itself.

## Checks

```bash
uv run pytest                        # add --cov for coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy app
```

Integration tests use `postgresql://selltrack:selltrack@localhost:5432/selltrack_test` and Redis
database 15. Override them with `TEST_DATABASE_URL` and `TEST_REDIS_URL`. They fail, rather than
skip, when either service is down. The test database is wiped on every run, so the tests refuse
to run against a database whose name doesn't contain `test`.

Browser tests (`tests/e2e/`) run axe-core and keyboard checks on every page, in light and dark, at
desktop width and at 360px. They run last, and need `npm run build` and a browser:
`uv run playwright install chromium`, or set `E2E_CHROMIUM_PATH` to use a Chromium you already
have. To skip them, use `uv run pytest -m "not e2e"`.

## Configuration

Every setting is listed and explained in [`.env.example`](.env.example). `APP_ENV` chooses between
`development`, `testing`, `staging` and `production`.

Docker, deployment and full documentation arrive in phase 6.

## Admin and background jobs

```bash
uv run flask users create-admin   # the first admin; the console is at /admin
uv run flask jobs scheduler       # keeps running: does the nightly cleanup, and stops on SIGTERM
uv run flask jobs cleanup         # the same cleanup, once, right now
```

The cleanup removes sales deleted over 30 days ago, sign-ins older than 90 days (apart from each
account's latest 20) and read notifications older than 180 days. The privacy page promises this, so
keep them in step. It runs at 17:00 UTC, or as soon as the scheduler is back if it was down then.

The admin console shows account details and counts, never what's in an account. Audit entries hold
no customer names, names of people in a split, or amounts, because they outlive the account. Admin
pages only show the detail fields listed in `_VISIBLE_DETAILS` in `app/services/audit.py`.

Dashboard totals are cached in Redis for up to five minutes. A commit that touches an account's
sales, costs or products makes its cached figures unreachable straight away. See
`app/figures_cache.py`. A bulk `update()` that changes figures without a changed `Sale`, `UpfrontCost`
or `Product` in the same transaction needs `figures_cache.forget(user_id)`.
