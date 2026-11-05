"""Sign-up, login, logout and account recovery."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, logout_user
from werkzeug.wrappers import Response

from app.extensions import limiter
from app.forms.auth import ActionForm, LoginForm, RecoverForm, RegisterForm
from app.security import rate_limits
from app.security.sessions import is_safe_redirect, start_session
from app.services import auth as auth_service
from app.services.errors import (
    AccountSuspendedError,
    AuthenticationError,
    ValidationFailed,
)

bp = Blueprint("auth", __name__)


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit(rate_limits.REGISTER_PER_IP, methods=["POST"])
def register() -> Response | str | tuple[str, int]:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    form = RegisterForm()
    if form.validate_on_submit():
        try:
            account = auth_service.register(form.username.data, form.password.data)
        except ValidationFailed as error:
            _show_field_errors(form, error)
        else:
            start_session(account.user)
            # Rendered from this response and never stored: this is the only time anyone,
            # including the server, can see these codes.
            return render_template(
                "pages/auth/recovery_codes.html",
                codes=account.recovery_codes,
                continue_url=url_for("dashboard.home"),
            )
    return render_template("pages/auth/register.html", form=form), _status(form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(rate_limits.LOGIN_PER_IP, methods=["POST"])
@limiter.limit(
    rate_limits.LOGIN_PER_USERNAME,
    methods=["POST"],
    key_func=rate_limits.submitted_username,
    deduct_when=rate_limits.attempt_failed,
)
def login() -> Response | str | tuple[str, int]:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = auth_service.authenticate(form.username.data, form.password.data)
        except AuthenticationError as error:
            flash(str(error), "error")
        except AccountSuspendedError as error:
            flash(f"{error} Please contact the site admin.", "error")
        else:
            start_session(user)
            target = request.args.get("next")
            if target and is_safe_redirect(target):
                return redirect(target)
            return redirect(url_for("dashboard.home"))
        return render_template("pages/auth/login.html", form=form), 401
    return render_template("pages/auth/login.html", form=form), _status(form)


@bp.post("/logout")
def logout() -> Response:
    form = ActionForm()
    if form.validate_on_submit() and current_user.is_authenticated:
        auth_service.record_logout(current_user)
    logout_user()
    flash("You are logged out.", "good")
    return redirect(url_for("auth.login"))


@bp.route("/recover", methods=["GET", "POST"])
@limiter.limit(rate_limits.RECOVER_PER_IP, methods=["POST"])
@limiter.limit(
    rate_limits.RECOVER_PER_USERNAME,
    methods=["POST"],
    key_func=rate_limits.submitted_username,
    deduct_when=rate_limits.attempt_failed,
)
def recover() -> Response | str | tuple[str, int]:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    form = RecoverForm()
    if form.validate_on_submit():
        try:
            auth_service.recover_account(form.username.data, form.code.data, form.new_password.data)
        except ValidationFailed as error:
            _show_field_errors(form, error)
        except AuthenticationError as error:
            flash(str(error), "error")
            return render_template("pages/auth/recover.html", form=form), 401
        except AccountSuspendedError as error:
            flash(f"{error} Please contact the site admin.", "error")
            return render_template("pages/auth/recover.html", form=form), 403
        else:
            flash("Your password has been changed. Log in with your new password.", "good")
            return redirect(url_for("auth.login"))
    return render_template("pages/auth/recover.html", form=form), _status(form)


def _show_field_errors(
    form: LoginForm | RecoverForm | RegisterForm, error: ValidationFailed
) -> None:
    for name, message in error.errors.items():
        field = getattr(form, name, None)
        if field is not None:
            field.errors = [*field.errors, message]
        else:
            flash(message, "error")


def _status(form: LoginForm | RecoverForm | RegisterForm) -> int:
    """A form sent back with mistakes is a 422, which is also what HTMX needs in phase 4."""
    return 422 if form.errors else 200
