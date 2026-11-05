"""Health checks for Docker, the deploy script and uptime monitors. No details are exposed."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify

from app.extensions import limiter
from app.services import system

bp = Blueprint("health", __name__)


@bp.get("/healthz")
@limiter.exempt
def healthz() -> Response:
    """Is the process up? Checks nothing else, so a database blip doesn't get it restarted."""
    return jsonify(status="ok")


@bp.get("/readyz")
@limiter.exempt
def readyz() -> tuple[Response, int]:
    """Can this instance serve requests? Without Redis it can, using in-memory rate limits."""
    database_ok = system.database_ok()
    redis_status = system.redis_status()
    status_code = 200 if database_ok else 503
    return (
        jsonify(
            status="ok" if database_ok else "unavailable",
            database="ok" if database_ok else "unavailable",
            redis=redis_status,
        ),
        status_code,
    )
