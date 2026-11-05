"""Login sessions.

The cookie holds ``"<user id>:<session version>"``. A session only counts while
the version matches the database, so bumping ``User.session_version`` logs the
account out everywhere at once. That is what makes a signed cookie revocable.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Flask, flash, redirect, request, session, url_for
from flask_login import current_user, login_user
from werkzeug.wrappers import Response

from app.extensions import db, login_manager
from app.models import User

_LAST_CONTROL_OR_SPACE = 0x20
#: What someone can still reach while they have a temporary password to replace.
_ALLOWED_WHILE_PASSWORD_EXPIRED = {
    "auth.logout",
    "health.healthz",
    "health.readyz",
    "settings.change_password",
    "static",
}


def init_app(app: Flask) -> None:
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in first."
    login_manager.login_message_category = "error"
    # "strong" ties a session to the IP address and logs people out whenever a phone changes
    # networks. Revocation is handled by the session version instead.
    login_manager.session_protection = None
    login_manager.user_loader(load_user)

    @app.before_request
    def require_password_change() -> Response | None:
        if "_user_id" not in session or request.endpoint in _ALLOWED_WHILE_PASSWORD_EXPIRED:
            return None
        if current_user.is_authenticated and current_user.must_change_password:
            flash("Please choose a new password to continue.", "error")
            return redirect(url_for("settings.change_password"))
        return None


def load_user(session_value: str) -> User | None:
    """Return the logged-in user, or ``None`` if the session is no longer valid."""
    user_id, separator, version = session_value.partition(":")
    if not separator or not user_id.isdecimal() or not version.isdecimal():
        return None
    user = db.session.get(User, int(user_id))
    if user is None or not user.is_active or user.session_version != int(version):
        return None
    return user


def start_session(user: User) -> None:
    """Log ``user`` in on a brand new session, so a cookie set before login is worthless."""
    session.clear()
    login_user(user)
    session.permanent = True


def is_safe_redirect(target: str | None) -> bool:
    """True only for a path on this site, so ``?next=`` can't send someone elsewhere after login."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    # Browsers strip tabs and newlines from URLs, which would turn "/\t/evil.example" into
    # "//evil.example".
    if any(ord(character) <= _LAST_CONTROL_OR_SPACE for character in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc
