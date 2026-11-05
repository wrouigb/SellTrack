"""Rate limits for anything a password or recovery code could be guessed through."""

from __future__ import annotations

import hashlib
from http import HTTPStatus

from flask import request
from flask_login import current_user
from werkzeug.wrappers import Response

LOGIN_PER_IP = "20 per minute"
#: Only failed attempts count, so someone logging in normally never hits this.
LOGIN_PER_USERNAME = "5 per minute;30 per hour"
REGISTER_PER_IP = "10 per hour"
RECOVER_PER_IP = "10 per hour"
RECOVER_PER_USERNAME = "5 per hour"
#: Changing a password or recovery codes asks for the current password.
PASSWORD_CHECK_PER_ACCOUNT = "10 per minute"
#: Only messages that were actually sent count, so a form with a mistake in it costs nothing.
SUPPORT_PER_ACCOUNT = "5 per hour"
#: Building the zip of everything in an account is the heaviest thing a person can ask for.
EXPORT_PER_ACCOUNT = "10 per hour"


def submitted_username() -> str:
    """Rate-limit key for the username in the form.

    Hashed because people sometimes type their password into the username box, and this ends
    up stored in Redis.
    """
    username = request.form.get("username", "").strip().casefold()
    return "username:" + hashlib.sha256(username.encode()).hexdigest()


def current_account() -> str:
    # The account ID alone: the session version changes when a password does, and that mustn't
    # reset the count. Limits run before the login check, so there may be nobody logged in.
    user_id = (current_user.get_id() or "").partition(":")[0]
    return f"account:{user_id}" if user_id else f"ip:{request.remote_addr}"


def form_accepted(response: Response) -> bool:
    return response.status_code == HTTPStatus.FOUND


def attempt_failed(response: Response) -> bool:
    """A successful form post redirects; anything else was a failed attempt."""
    return response.status_code != HTTPStatus.FOUND
