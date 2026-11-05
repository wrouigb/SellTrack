"""Health checks for Docker, the deploy script and uptime monitors. No details are exposed."""

from __future__ import annotations

import redis
import structlog
from flask import Blueprint, Response, current_app, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db, limiter

bp = Blueprint("health", __name__)
log = structlog.get_logger("selltrack.health")

_REDIS_TIMEOUT_SECONDS = 1


@bp.get("/healthz")
@limiter.exempt
def healthz() -> Response:
    """Is the process up? Checks nothing else, so a database blip doesn't get it restarted."""
    return jsonify(status="ok")


@bp.get("/readyz")
@limiter.exempt
def readyz() -> tuple[Response, int]:
    """Can this instance serve requests? Without Redis it can, using in-memory rate limits."""
    database_ok = _database_ok()
    redis_status = _redis_status()
    status_code = 200 if database_ok else 503
    return (
        jsonify(
            status="ok" if database_ok else "unavailable",
            database="ok" if database_ok else "unavailable",
            redis=redis_status,
        ),
        status_code,
    )


def _database_ok() -> bool:
    try:
        db.session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as error:
        log.error("health.database_unavailable", error=type(error).__name__)
        db.session.rollback()
        return False
    return True


def _redis_status() -> str:
    url = current_app.config["REDIS_URL"]
    if not url:
        return "not_configured"
    try:
        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        try:
            client.ping()
        finally:
            client.close()
    except (redis.RedisError, OSError) as error:
        log.warning("health.redis_unavailable", error=type(error).__name__)
        return "degraded"
    return "ok"
