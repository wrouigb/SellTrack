"""Configuration, selected by ``APP_ENV`` and read from the environment.

Staging and production refuse to start without a strong ``SECRET_KEY``, a
Postgres URL and debug off: the original app fell back to a hard-coded secret,
which let anyone forge a login cookie (plan section 2.4, issue 1).
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.extensions import JsonSerializer

Environment = Literal["development", "testing", "staging", "production"]

MIN_SECRET_BYTES = 32
_MIN_SECRET_DISTINCT_CHARS = 8
_PLACEHOLDER_MARKERS = ("change-this", "change-me", "changeme", "replace-me", "your-secret")
_POSTGRES_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg"}


class ConfigError(RuntimeError):
    """The environment can't safely run the app. Raised at startup, never at request time."""


class Settings(BaseSettings):
    """Values read from environment variables (or a local ``.env`` file)."""

    # `env_ignore_empty`: "SENTRY_DSN=" in a .env file means not set, rather than an empty string.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: Environment = "development"
    secret_key: SecretStr | None = None
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    debug: bool = False
    #: How many reverse proxies sit in front of Gunicorn. Client IPs (and so rate limits and
    #: sign-in history) are only right when this matches the deployment.
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)
    log_level: str = "INFO"
    sentry_dsn: SecretStr | None = None
    #: Where nightly backups are written, for the admin's system page to check on.
    backup_dir: Path | None = None


class BaseConfig:
    """Flask settings shared by every environment."""

    SECRET_KEY: ClassVar[str | None] = None
    DEBUG = False
    TESTING = False

    # Cookies
    SESSION_COOKIE_NAME = "selltrack_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    # No uploads anywhere in the app, so bodies are only ever form posts.
    MAX_CONTENT_LENGTH = 1024 * 1024

    # A CSRF token lives as long as the session; a form left open for an hour shouldn't fail.
    WTF_CSRF_TIME_LIMIT = None

    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict[str, Any]] = {
        "pool_pre_ping": True,
        "pool_size": 5,  # small enough for a Raspberry Pi
        "max_overflow": 5,
        "connect_args": {"connect_timeout": 5},
    }

    # Rate limits: a Redis outage falls back to per-process counting rather than failing logins.
    RATELIMIT_STRATEGY = "fixed-window"
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True
    RATELIMIT_KEY_PREFIX = "selltrack"
    RATELIMIT_STORAGE_OPTIONS: ClassVar[dict[str, Any]] = {
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }

    # Dashboard figures. Without Redis nothing is cached, and the dashboard just asks the database.
    CACHE_DEFAULT_TIMEOUT = 300
    CACHE_KEY_PREFIX = "selltrack:cache:"
    CACHE_NO_NULL_WARNING = True
    CACHE_OPTIONS: ClassVar[dict[str, Any]] = {
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }

    # Argon2id. These are RFC 9106's low-memory recommendation (64 MiB), fine on a Raspberry Pi.
    ARGON2_TIME_COST = 3
    ARGON2_MEMORY_COST_KIB = 65_536
    ARGON2_PARALLELISM = 4

    HSTS_ENABLED = False
    #: Structured logs: JSON in deployed environments, readable in a terminal otherwise.
    LOG_JSON = True


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    LOG_JSON = False


class TestingConfig(BaseConfig):
    TESTING = True
    # Most tests post forms directly. The tests of CSRF itself turn it back on.
    WTF_CSRF_ENABLED = False
    RATELIMIT_KEY_PREFIX = "selltrack-test"
    CACHE_KEY_PREFIX = "selltrack-test:cache:"
    # The cheapest settings Argon2 allows, so that hundreds of test logins stay quick.
    ARGON2_TIME_COST = 1
    ARGON2_MEMORY_COST_KIB = 8
    ARGON2_PARALLELISM = 1


class StagingConfig(BaseConfig):
    # "__Host-" makes browsers refuse the cookie unless it is Secure, host-only and Path=/.
    SESSION_COOKIE_NAME = "__Host-selltrack_session"
    SESSION_COOKIE_SECURE = True
    HSTS_ENABLED = True


class ProductionConfig(StagingConfig):
    pass


_CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "staging": StagingConfig,
    "production": ProductionConfig,
}
_STRICT_ENVIRONMENTS = {"staging", "production"}


def is_strict(app_env: str) -> bool:
    """True for environments that hold real data and must fail closed."""
    return app_env in _STRICT_ENVIRONMENTS


def normalise_database_url(url: str) -> str:
    """Return ``url`` using the psycopg 3 driver. Only Postgres is supported, even in tests."""
    scheme, separator, rest = url.partition("://")
    if not separator or scheme not in _POSTGRES_SCHEMES:
        raise ConfigError("DATABASE_URL must be a PostgreSQL URL, like postgresql://host/name.")
    return f"postgresql+psycopg://{rest}"


def check_secret_strength(secret: str) -> None:
    """Raise :class:`ConfigError` unless ``secret`` is safe to sign session cookies with."""
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ConfigError(f"SECRET_KEY must be at least {MIN_SECRET_BYTES} bytes long.")
    if any(marker in secret.lower() for marker in _PLACEHOLDER_MARKERS):
        raise ConfigError("SECRET_KEY is still a placeholder. Generate a random one.")
    if len(set(secret)) < _MIN_SECRET_DISTINCT_CHARS:
        raise ConfigError("SECRET_KEY doesn't look random. Generate a random one.")


def build_flask_config(settings: Settings) -> dict[str, Any]:
    """Validate ``settings`` and return the mapping to load into ``app.config``."""
    env = settings.app_env
    strict = is_strict(env)

    if settings.database_url is None:
        raise ConfigError("DATABASE_URL is required.")
    if strict and settings.debug:
        raise ConfigError(f"DEBUG can't be on when APP_ENV is {env}.")

    secret = settings.secret_key.get_secret_value() if settings.secret_key else None
    if strict:
        if secret is None:
            raise ConfigError(f"SECRET_KEY is required when APP_ENV is {env}.")
        check_secret_strength(secret)
    elif secret is None:
        # Never a fixed fallback: logins just don't survive a restart of a dev server.
        secret = secrets.token_urlsafe(MIN_SECRET_BYTES)

    base = _CONFIGS[env]
    config = {name: getattr(base, name) for name in dir(base) if name.isupper()}
    config["SECRET_KEY"] = secret
    config["APP_ENV"] = env
    config["SQLALCHEMY_DATABASE_URI"] = normalise_database_url(
        settings.database_url.get_secret_value()
    )
    config["RATELIMIT_STORAGE_URI"] = (
        settings.redis_url.get_secret_value() if settings.redis_url else "memory://"
    )
    config["REDIS_URL"] = settings.redis_url.get_secret_value() if settings.redis_url else None
    config["CACHE_TYPE"] = "RedisCache" if settings.redis_url else "NullCache"
    config["CACHE_REDIS_URL"] = config["REDIS_URL"]
    config["CACHE_SERIALIZER"] = JsonSerializer if settings.redis_url else None
    config["BACKUP_DIR"] = settings.backup_dir
    config["TRUSTED_PROXY_COUNT"] = settings.trusted_proxy_count
    config["LOG_LEVEL"] = settings.log_level.upper()
    config["SENTRY_DSN"] = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else None
    if settings.debug:
        config["DEBUG"] = True
    return config
