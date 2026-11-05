"""Public pages. The real landing, help and legal pages arrive in phase 4."""

from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("public", __name__)


@bp.get("/")
def landing() -> str:
    return render_template("pages/landing.html")
