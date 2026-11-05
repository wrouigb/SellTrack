# SellTrack

Track what you sell, see what you make, and split the profit fairly.

> **Status: being rebuilt for production.** The backend is done (phase 3 of the plan in
> [`docs/plan.md`](docs/plan.md)). The pages are placeholders until phase 4, so the sales,
> products and split features can only be used through their services for now. The original
> Flask/SQLite app is kept in [`legacy/`](legacy/) as the reference for those pages.

## What's here

| Path | What it is |
|---|---|
| `app/__init__.py`, `app/config.py` | App factory and configuration. Staging and production refuse to start without a strong secret, Postgres and debug off |
| `app/models/` | The database schema (plan section 7) |
| `migrations/` | Alembic migrations |
| `app/money.py` | Exact AUD money: strict parsing, whole cents, en-AU formatting, totals that never lose a cent |
| `app/services/` | The business rules: accounts, products, sales, reporting, the profit split, notifications, feature flags, audit log |
| `app/security/` | Sessions, passwords (Argon2id), permissions, security headers, rate limits |
| `app/blueprints/`, `app/forms/`, `app/templates/` | Thin routes and plain pages for sign-up, login, recovery and account security |
| `tests/` | Unit tests, and integration tests on real Postgres and Redis |

## Setting up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 and Redis.

```bash
uv sync
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

## Configuration

Every setting is listed and explained in [`.env.example`](.env.example). `APP_ENV` chooses between
`development`, `testing`, `staging` and `production`.

Docker, deployment and full documentation arrive in phase 6.
