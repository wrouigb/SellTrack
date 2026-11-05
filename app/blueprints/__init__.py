"""Routes, grouped by area. These stay thin: the rules are in ``app.services``."""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from app.blueprints import auth, dashboard, health, public, settings

    for module in (public, auth, dashboard, settings, health):
        app.register_blueprint(module.bp)
