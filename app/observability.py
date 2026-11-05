"""Structured logs, request IDs and optional error reporting."""

from __future__ import annotations

import logging
import re
import sys
import uuid
from time import perf_counter

import structlog
from flask import Flask, Response, g, has_request_context, request, session

_REQUEST_ID_HEADER = "X-Request-ID"
# An inbound ID ends up in logs and a response header, so anything unusual is replaced rather
# than trusted: this is what stops a client injecting fake log lines.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_QUIET_ENDPOINTS = {"health.healthz", "health.readyz", "static"}

log = structlog.get_logger("selltrack.request")


def init_app(app: Flask) -> None:
    configure_logging(json_logs=app.config["LOG_JSON"], level=app.config["LOG_LEVEL"])
    _init_sentry(app)

    @app.before_request
    def start_request() -> None:
        inbound = request.headers.get(_REQUEST_ID_HEADER, "")
        g.request_id = inbound if _SAFE_REQUEST_ID.fullmatch(inbound) else str(uuid.uuid4())
        g.request_started = perf_counter()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=g.request_id)

    @app.after_request
    def finish_request(response: Response) -> Response:
        if request_id := g.get("request_id"):
            response.headers[_REQUEST_ID_HEADER] = request_id
        started = g.get("request_started")
        if started is not None:
            level = "debug" if request.endpoint in _QUIET_ENDPOINTS else "info"
            getattr(log, level)(
                "request.finished",
                method=request.method,
                # The path only: query strings can hold search terms typed by the user.
                path=request.path,
                status=response.status_code,
                duration_ms=round((perf_counter() - started) * 1000, 1),
                user_id=_session_user_id(),
            )
        return response


def current_request_id() -> str | None:
    return g.get("request_id") if has_request_context() else None


def _session_user_id() -> int | None:
    # Read from the cookie, not `current_user`: this also runs on error pages, when the
    # database may be the very thing that is down.
    raw = session.get("_user_id")
    if not isinstance(raw, str):
        return None
    user_id, _, _ = raw.partition(":")
    return int(user_id) if user_id.isdecimal() else None


def configure_logging(*, json_logs: bool, level: str) -> None:
    """Send everything, including stdlib loggers, to stdout as one structured format."""
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
    )
    final: list[structlog.types.Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta
    ]
    if json_logs:
        # show_locals=False matters: by default every frame's local variables are written to the
        # log, and in this app those include things like `password`.
        final.append(
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            )
        )
    final.append(renderer)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=final)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _init_sentry(app: Flask) -> None:
    dsn = app.config["SENTRY_DSN"]
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=app.config["APP_ENV"],
        send_default_pii=False,  # no usernames, IPs or cookies leave the server
        traces_sample_rate=0.0,
    )
