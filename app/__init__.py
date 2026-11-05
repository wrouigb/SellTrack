"""SellTrack: track what you sell, see what you make, and split the profit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from flask import Flask
from flask.helpers import get_debug_flag
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter, Map

from app.config import ConfigError, Settings, build_flask_config, is_strict

MAX_BIGINT = 2**63 - 1


class DatabaseIdConverter(IntegerConverter):
    """``<int:id>`` in a URL. A number too big to be an ID is simply a page that doesn't exist."""

    def __init__(self, url_map: Map, **kwargs: Any) -> None:
        super().__init__(url_map, **{"max": MAX_BIGINT, **kwargs})


def create_app(
    settings: Settings | None = None, overrides: Mapping[str, Any] | None = None
) -> Flask:
    """Build the application. Raises ``ConfigError`` when the environment isn't safe to run."""
    # Imported here so that pure modules such as ``app.money`` don't drag in the web stack.
    from app import assets, cli, errors, models, observability, templating  # noqa: F401
    from app.blueprints import register_blueprints
    from app.extensions import csrf, db, limiter, login_manager, migrate
    from app.security import headers, sessions

    app = Flask(__name__)
    app.url_map.converters["int"] = DatabaseIdConverter
    app.config.from_mapping(build_flask_config(settings or Settings()))
    if overrides:
        app.config.from_mapping(overrides)

    # FLASK_DEBUG or `flask --debug` turn on the Werkzeug debugger, which is remote code
    # execution for anyone who can reach it. Debug is impossible outside development. The flag
    # is checked as well as app.debug because `flask run` reads it directly.
    if is_strict(app.config["APP_ENV"]) and (app.debug or get_debug_flag()):
        raise ConfigError(f"Debug mode can't be on when APP_ENV is {app.config['APP_ENV']}.")

    proxies = app.config["TRUSTED_PROXY_COUNT"]
    if proxies:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies
        )

    observability.init_app(app)
    if is_strict(app.config["APP_ENV"]) and not app.config["REDIS_URL"]:
        structlog.get_logger("selltrack.startup").warning(
            "config.no_redis", detail="Each worker counts rate limits on its own, in memory."
        )

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True, render_as_batch=False)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    sessions.init_app(app)
    headers.init_app(app)
    errors.init_app(app)
    templating.init_app(app)
    assets.init_app(app)
    register_blueprints(app)
    cli.init_app(app)
    return app
