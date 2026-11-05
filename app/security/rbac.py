"""Permission checks for views. Guests go to the login page; anyone else without access gets 403."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import cast

from flask import abort, current_app
from flask.typing import ResponseReturnValue
from flask_login import current_user
from flask_login import login_required as _flask_login_required


def login_required[**P, R](view: Callable[P, R]) -> Callable[P, R]:
    """Only a logged-in user with a valid session may call this view."""
    return cast("Callable[P, R]", _flask_login_required(view))


def only_admins() -> ResponseReturnValue | None:
    """A ``before_request`` check for a whole blueprint, so no route can be left unguarded."""
    if not current_user.is_authenticated:
        return cast("ResponseReturnValue", current_app.login_manager.unauthorized())  # type: ignore[attr-defined]
    if not current_user.is_admin:
        abort(403)
    return None


def admin_required[**P, R](view: Callable[P, R]) -> Callable[P, R]:
    """Only an active admin may call this view."""

    @wraps(view)
    @login_required
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
