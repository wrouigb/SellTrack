"""The signed-in home page. A placeholder until the dashboard is built in phase 4."""

from __future__ import annotations

from flask import Blueprint, render_template

from app.security.rbac import login_required

bp = Blueprint("dashboard", __name__, url_prefix="/app")


@bp.get("")
@login_required
def home() -> str:
    return render_template("pages/home.html")
