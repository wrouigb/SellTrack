"""Shared fixtures.

Integration tests run against real Postgres and Redis, as production does. They read
``TEST_DATABASE_URL`` and ``TEST_REDIS_URL``; see the defaults below. The database's contents are
replaced, so never point these at anything you care about.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest
import redis
import sqlalchemy as sa
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from app.config import Settings, normalise_database_url
from app.extensions import db

DEFAULT_DATABASE_URL = "postgresql://selltrack:selltrack@localhost:5432/selltrack_test"
DEFAULT_REDIS_URL = "redis://localhost:6379/15"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run browser tests last: they're the slowest, and they keep their own data in the database."""
    items.sort(key=lambda item: "e2e" in item.path.parts)


def database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


def redis_url() -> str:
    return os.environ.get("TEST_REDIS_URL", DEFAULT_REDIS_URL)


def make_settings(**values: Any) -> Settings:
    return Settings(
        _env_file=None,
        app_env=values.pop("app_env", "testing"),
        database_url=values.pop("database_url", database_url()),
        redis_url=values.pop("redis_url", redis_url()),
        **values,
    )


def _require_services() -> None:
    """Fail loudly, because silently skipping would make a broken setup look like a pass."""
    url = database_url()
    if "test" not in url.rsplit("/", 1)[-1]:
        pytest.exit(f"Refusing to wipe a database whose name doesn't contain 'test': {url}")
    try:
        engine = sa.create_engine(normalise_database_url(url), connect_args={"connect_timeout": 3})
        with engine.connect():
            pass
        engine.dispose()
    except sa.exc.OperationalError as error:
        pytest.fail(f"Postgres isn't reachable (set TEST_DATABASE_URL): {error}", pytrace=False)
    try:
        redis.Redis.from_url(redis_url(), socket_connect_timeout=3).ping()
    except redis.RedisError as error:
        pytest.fail(f"Redis isn't reachable (set TEST_REDIS_URL): {error}", pytrace=False)


@pytest.fixture(scope="session")
def app() -> Iterator[Flask]:
    _require_services()
    application = create_app(make_settings())
    with application.app_context():
        _rebuild_schema()
    return application


def _rebuild_schema() -> None:
    """Empty the database and build it with the real migrations, which tests them too."""
    from flask_migrate import upgrade

    with db.engine.begin() as connection:
        connection.execute(sa.text("DROP SCHEMA public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))
    upgrade()


@pytest.fixture
def clean_state(app: Flask) -> Iterator[None]:
    """Every test starts with empty tables and fresh rate-limit counters."""
    yield
    with app.app_context():
        db.session.remove()
        tables = ", ".join(f'"{table.name}"' for table in db.metadata.sorted_tables)
        with db.engine.begin() as connection:
            connection.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    redis.Redis.from_url(redis_url()).flushdb()


@pytest.fixture
def ctx(app: Flask, clean_state: None) -> Iterator[None]:
    """An application context, for tests that call services directly."""
    with app.app_context():
        yield
        db.session.remove()


@pytest.fixture
def client(app: Flask, clean_state: None) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def csrf_client(clean_state: None) -> Iterator[FlaskClient]:
    """A client against an app with CSRF protection on, exactly as in production."""
    application = create_app(make_settings(), overrides={"WTF_CSRF_ENABLED": True})
    return application.test_client()


CSRF_FIELD = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


def csrf_token_from(html: str) -> str:
    match = CSRF_FIELD.search(html)
    assert match, "no CSRF token in the page"
    return match.group(1)


def login(client: FlaskClient, username: str, password: str, **kwargs: Any) -> Any:
    return client.post("/login", data={"username": username, "password": password}, **kwargs)


def register(client: FlaskClient, username: str, password: str, **kwargs: Any) -> Any:
    return client.post(
        "/register",
        data={"username": username, "password": password, "confirm": password},
        **kwargs,
    )


RECOVERY_CODE = re.compile(r"<code>([0-9A-Z]{4}(?:-[0-9A-Z]{4}){3})</code>")


def recovery_codes_in(html: str) -> list[str]:
    return RECOVERY_CODE.findall(html)
