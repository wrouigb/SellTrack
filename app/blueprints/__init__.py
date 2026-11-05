"""Routes, grouped by area. These stay thin: the rules are in ``app.services``."""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from app.blueprints import (
        auth,
        dashboard,
        health,
        onboarding,
        products,
        public,
        sales,
        settings,
        split,
    )

    for module in (public, auth, onboarding, dashboard, sales, products, split, settings, health):
        app.register_blueprint(module.bp)
