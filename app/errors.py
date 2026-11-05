"""Error pages. They show a reference ID and nothing about what went wrong."""

from __future__ import annotations

import structlog
from flask import Flask, Response, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from sqlalchemy.exc import DataError
from werkzeug.exceptions import HTTPException, InternalServerError

from app.observability import current_request_id
from app.services.errors import LockedError, NotFoundError

log = structlog.get_logger("selltrack.errors")

_MESSAGES: dict[int, tuple[str, str]] = {
    400: ("That didn't work", "The request couldn't be understood. Go back and try again."),
    403: ("Not allowed", "You don't have access to this page."),
    404: ("Page not found", "This page doesn't exist, or it isn't part of your account."),
    405: ("That didn't work", "This page can't be used that way."),
    409: ("That didn't work", "This has been settled, so it can't be changed."),
    413: ("Too much data", "That was more data than this page accepts."),
    429: ("Too many attempts", "Please wait a little while, then try again."),
    500: ("Something went wrong", "It wasn't your fault. Please try again in a moment."),
}


def init_app(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response | str, int]:
        code = error.code or 500
        title, message = _MESSAGES.get(code, _MESSAGES[400 if code < 500 else 500])
        return _render(code, title, message)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error: CSRFError) -> tuple[Response | str, int]:
        log.warning("security.csrf_rejected", path=request.path, reason=error.description)
        return _render(
            400,
            "Please try that again",
            "This page had been open too long or came from somewhere else. "
            "Go back, reload the page and try again.",
        )

    # Registered for 500 rather than Exception: Flask then still logs the traceback and sends
    # the signal that Sentry listens for before calling this.
    @app.errorhandler(InternalServerError)
    def handle_server_error(_error: InternalServerError) -> tuple[Response | str, int]:
        title, message = _MESSAGES[500]
        return _render(500, title, message)

    # A value the database can't hold, such as an ID from a tampered form that's too big for a
    # bigint. Whoever sent it made a bad request; it isn't a fault in the app.
    @app.errorhandler(DataError)
    def handle_data_error(error: DataError) -> tuple[Response | str, int]:
        log.warning("request.bad_value", path=request.path, error=type(error.orig).__name__)
        title, message = _MESSAGES[400]
        return _render(400, title, message)

    @app.errorhandler(NotFoundError)
    def handle_not_found(_error: NotFoundError) -> tuple[Response | str, int]:
        title, message = _MESSAGES[404]
        return _render(404, title, message)

    @app.errorhandler(LockedError)
    def handle_locked(error: LockedError) -> tuple[Response | str, int]:
        title, _ = _MESSAGES[409]
        return _render(409, title, str(error))


def _render(code: int, title: str, message: str) -> tuple[Response | str, int]:
    reference = current_request_id()
    if request.accept_mimetypes.best == "application/json":
        return jsonify(error=title, message=message, reference=reference), code
    # The template extends a layout that never touches the database, which may be what failed.
    return render_template(
        "pages/error.html", code=code, title=title, message=message, reference=reference
    ), code
