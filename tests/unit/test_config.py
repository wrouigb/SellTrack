"""The app must refuse to start when the environment isn't safe (audit issues 1 and 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from app.config import (
    ConfigError,
    Settings,
    build_flask_config,
    check_secret_strength,
    normalise_database_url,
)

GOOD_SECRET = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3Bx"
POSTGRES = "postgresql://user:password@db.example/selltrack"


def settings(**values) -> Settings:
    values.setdefault("database_url", POSTGRES)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("env", ["staging", "production"])
class TestStrictEnvironments:
    def test_starts_with_good_settings(self, env):
        config = build_flask_config(settings(app_env=env, secret_key=GOOD_SECRET))
        assert config["SECRET_KEY"] == GOOD_SECRET
        assert config["DEBUG"] is False
        assert config["SESSION_COOKIE_SECURE"] is True
        assert config["SESSION_COOKIE_NAME"].startswith("__Host-")
        assert config["HSTS_ENABLED"] is True

    def test_refuses_a_missing_secret(self, env):
        with pytest.raises(ConfigError, match="SECRET_KEY is required"):
            build_flask_config(settings(app_env=env))

    @pytest.mark.parametrize(
        "secret",
        [
            "short",
            "change-this-to-your-own-secret-text",  # the original app's hard-coded fallback
            "change-this-to-your-own-secret-text-and-make-it-long-enough",
            "a" * 64,
            "abababababababababababababababababab",
        ],
    )
    def test_refuses_a_weak_secret(self, env, secret):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            build_flask_config(settings(app_env=env, secret_key=secret))

    def test_refuses_debug(self, env):
        with pytest.raises(ConfigError, match="DEBUG"):
            build_flask_config(settings(app_env=env, secret_key=GOOD_SECRET, debug=True))

    def test_refuses_the_flask_debug_variable(self, env, monkeypatch):
        monkeypatch.setenv("FLASK_DEBUG", "1")
        with pytest.raises(ConfigError, match="Debug mode"):
            create_app(settings(app_env=env, secret_key=GOOD_SECRET))

    def test_refuses_a_debug_override(self, env):
        with pytest.raises(ConfigError, match="Debug mode"):
            create_app(settings(app_env=env, secret_key=GOOD_SECRET), overrides={"DEBUG": True})

    def test_refuses_sqlite(self, env):
        with pytest.raises(ConfigError, match="PostgreSQL"):
            build_flask_config(
                settings(app_env=env, secret_key=GOOD_SECRET, database_url="sqlite:///tracker.db")
            )

    def test_refuses_a_missing_database(self, env):
        with pytest.raises(ConfigError, match="DATABASE_URL"):
            build_flask_config(Settings(_env_file=None, app_env=env, secret_key=GOOD_SECRET))


class TestDevelopment:
    def test_generates_a_random_secret_when_none_is_set(self):
        first = build_flask_config(settings(app_env="development"))
        second = build_flask_config(settings(app_env="development"))
        assert len(first["SECRET_KEY"]) >= 32
        assert first["SECRET_KEY"] != second["SECRET_KEY"], "never a fixed fallback"

    def test_debug_allowed(self, monkeypatch):
        monkeypatch.setenv("FLASK_DEBUG", "1")
        app = create_app(settings(app_env="development"))
        assert app.debug is True

    def test_cookies_work_over_plain_http(self):
        config = build_flask_config(settings(app_env="development"))
        assert config["SESSION_COOKIE_SECURE"] is False
        assert config["HSTS_ENABLED"] is False


class TestEveryEnvironment:
    @pytest.mark.parametrize("env", ["development", "testing", "staging", "production"])
    def test_cookie_defaults(self, env):
        config = build_flask_config(settings(app_env=env, secret_key=GOOD_SECRET))
        assert config["SESSION_COOKIE_HTTPONLY"] is True
        assert config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_secrets_are_hidden_when_settings_are_printed(self):
        printable = repr(settings(secret_key=GOOD_SECRET))
        assert GOOD_SECRET not in printable
        assert "password" not in printable

    def test_unknown_environment_is_refused(self):
        with pytest.raises(ValueError, match="app_env"):
            settings(app_env="prod")


class TestDatabaseUrl:
    @pytest.mark.parametrize(
        "url",
        ["postgres://h/d", "postgresql://h/d", "postgresql+psycopg://h/d"],
    )
    def test_uses_the_psycopg_driver(self, url):
        assert normalise_database_url(url) == "postgresql+psycopg://h/d"

    @pytest.mark.parametrize("url", ["sqlite:///x.db", "mysql://h/d", "not a url", ""])
    def test_refuses_anything_else(self, url):
        with pytest.raises(ConfigError):
            normalise_database_url(url)


def test_env_example_lists_every_setting():
    example = Path(__file__).parents[2].joinpath(".env.example").read_text()
    listed = {line.split("=")[0].lower() for line in example.splitlines() if "=" in line}
    listed = {name for name in listed if not name.startswith("#")}
    assert listed == set(Settings.model_fields)


def test_a_good_secret_passes():
    check_secret_strength(GOOD_SECRET)


def test_redis_url_is_optional_and_falls_back_to_memory():
    config = build_flask_config(settings())
    assert config["RATELIMIT_STORAGE_URI"] == "memory://"
