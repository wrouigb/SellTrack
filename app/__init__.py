"""SellTrack: track what you sell, see what you make, and split the profit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import ConfigError, Settings, build_flask_config, is_strict


def create_app(
    settings: Settings | None = None, overrides: Mapping[str, Any] | None = None
) -> Flask:
    """Build the application. Raises ``ConfigError`` when the environment isn't safe to run."""
    # Imported here so that pure modules such as ``app.money`` don't drag in the web stack.
    from app import cli, errors, models, observability, templating  # noqa: F401
    from app.blueprints import register_blueprints
    from app.extensions import csrf, db, limiter, login_manager, migrate
    from app.security import headers, sessions

    app = Flask(__name__)
    app.config.from_mapping(build_flask_config(settings or Settings()))
    if overrides:
        app.config.from_mapping(overrides)

    # FLASK_DEBUG or `flask --debug` turn on the Werkzeug debugger, which is remote code
    # execution for anyone who can reach it. Debug is impossible outside development.
    if is_strict(app.config["APP_ENV"]) and app.debug:
        raise ConfigError(f"Debug mode can't be on when APP_ENV is {app.config['APP_ENV']}.")

    proxies = app.config["TRUSTED_PROXY_COUNT"]
    if proxies:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies
        )

    observability.init_app(app)

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True, render_as_batch=False)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    sessions.init_app(app)
    headers.init_app(app)
    errors.init_app(app)
    templating.init_app(app)
    register_blueprints(app)
    cli.init_app(app)
    return app
