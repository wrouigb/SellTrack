# The everyday commands. `make help` lists them.
.DEFAULT_GOAL := help
.PHONY: help setup services test test-only lint format types audit check dev assets up down prod-config

DB_PORT ?= 5432
REDIS_PORT ?= 6379

export FLASK_APP := app:create_app
export TEST_DATABASE_URL ?= postgresql://selltrack:selltrack@localhost:$(DB_PORT)/selltrack_test
export TEST_ADMIN_DATABASE_URL ?= postgresql://postgres:postgres@localhost:$(DB_PORT)/postgres
export TEST_REDIS_URL ?= redis://localhost:$(REDIS_PORT)/15

help: ## Show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

setup: ## Install everything and build the CSS and JavaScript
	uv sync --frozen
	npm ci
	npm run build
	test -f .env || { cp .env.example .env; printf 'SECRET_KEY=%s\n' "$$(openssl rand -hex 32)" >> .env; }
	test -n "$$E2E_CHROMIUM_PATH" || uv run playwright install chromium

services: ## Start Postgres and Redis in Docker
	docker compose up --detach --wait db redis

test: services test-only ## Run every test, starting Postgres and Redis first

test-only: ## Run every test, with Postgres and Redis already running
	uv run pytest --cov=app --cov-report=term --cov-fail-under=85
	uv run coverage report --include='app/services/split_engine.py' --fail-under=95

lint: ## Check style and common mistakes
	uv run ruff check .
	uv run ruff format --check .

format: ## Fix style
	uv run ruff check --fix .
	uv run ruff format .

types: ## Check types
	uv run mypy app

audit: ## Look for dependencies with known vulnerabilities
	uv export --frozen --no-dev --no-hashes --output-file=.requirements-audit.txt >/dev/null
	uv run pip-audit --requirement=.requirements-audit.txt --no-deps --disable-pip; \
		status=$$?; rm -f .requirements-audit.txt; exit $$status
	npm audit --omit=dev

check: lint types audit ## Run all the quick checks

dev: services ## Run the app on http://localhost:5000, restarting when code changes
	uv run flask jobs migrate
	uv run flask run --debug

assets: ## Rebuild the CSS and JavaScript whenever they change
	npm run watch

up: ## Run the whole stack in Docker, on http://localhost:8080
	docker compose up --build

down: ## Stop the stack. Your data stays, unless you add -v to docker compose down
	docker compose down

prod-config: ## Check the production compose file, with made-up values
	IMAGE_REPO=example/selltrack IMAGE_TAG=check SECRET_KEY=x POSTGRES_PASSWORD=x DB_APP_PASSWORD=x \
		DB_MIGRATOR_PASSWORD=x DB_BACKUP_PASSWORD=x \
		docker compose --file deploy/compose.prod.yml config --quiet
