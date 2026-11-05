"""Settings: account, security, preferences and your data."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, logout_user
from werkzeug.wrappers import Response

from app.extensions import limiter
from app.forms import show_errors
from app.forms.auth import ChangePasswordForm, DeleteAccountForm, PasswordConfirmForm
from app.forms.records import PreferencesForm, timezone_options
from app.security import rate_limits
from app.security.rbac import login_required
from app.security.sessions import start_session
from app.services import auth as auth_service
from app.services.errors import ValidationFailed

bp = Blueprint("settings", __name__, url_prefix="/app/settings")

_INVALID = 422


@bp.get("")
@login_required
def account() -> str:
    return render_template("pages/settings/account.html", tab="account")


@bp.get("/security")
@login_required
def security() -> str:
    return render_template(
        "pages/settings/security.html",
        tab="security",
        sign_ins=auth_service.recent_sign_ins(current_user.id),
        codes_left=auth_service.remaining_recovery_codes(current_user.id),
        password_form=PasswordConfirmForm(),
    )


@bp.route("/security/password", methods=["GET", "POST"])
@login_required
@limiter.limit(
    rate_limits.PASSWORD_CHECK_PER_ACCOUNT, methods=["POST"], key_func=rate_limits.current_account
)
def change_password() -> Response | str | tuple[str, int]:
    form = ChangePasswordForm()
    if form.validate_on_submit():
        try:
            auth_service.change_password(
                current_user, form.current_password.data, form.new_password.data
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            # The version just changed, which ended this session along with the others.
            # Start a new one so that only the *other* devices are logged out.
            start_session(current_user._get_current_object())
            flash("Password changed. Your other devices have been logged out.", "good")
            return redirect(url_for("settings.security"))
    status = _INVALID if form.errors else 200
    return render_template("pages/settings/change_password.html", form=form, tab="security"), status


@bp.post("/security/recovery-codes")
@login_required
@limiter.limit(rate_limits.PASSWORD_CHECK_PER_ACCOUNT, key_func=rate_limits.current_account)
def regenerate_recovery_codes() -> Response | str:
    form = PasswordConfirmForm()
    if form.validate_on_submit():
        try:
            codes = auth_service.regenerate_recovery_codes(current_user, form.password.data)
        except ValidationFailed as error:
            flash(next(iter(error.errors.values())), "error")
        else:
            return render_template(
                "pages/auth/recovery_codes.html",
                codes=codes,
                continue_url=url_for("settings.security"),
            )
    return redirect(url_for("settings.security"))


@bp.post("/security/logout-everywhere")
@login_required
def logout_everywhere() -> Response:
    auth_service.logout_everywhere(current_user)
    logout_user()
    flash("You have been logged out on every device.", "good")
    return redirect(url_for("auth.login"))


@bp.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences() -> Response | str | tuple[str, int]:
    form = PreferencesForm()
    form.timezone.choices = timezone_options()
    if form.validate_on_submit():
        try:
            auth_service.update_preferences(
                current_user, timezone=form.timezone.data, theme=form.theme.data
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Preferences saved.", "good")
            return redirect(url_for("settings.preferences"))
    elif request.method == "GET":
        form.timezone.data = current_user.timezone
        form.theme.data = current_user.theme.value
    status = _INVALID if form.errors else 200
    return render_template("pages/settings/preferences.html", form=form, tab="preferences"), status


@bp.route("/data", methods=["GET", "POST"])
@login_required
@limiter.limit(
    rate_limits.PASSWORD_CHECK_PER_ACCOUNT, methods=["POST"], key_func=rate_limits.current_account
)
def data() -> Response | str | tuple[str, int]:
    form = DeleteAccountForm()
    if form.validate_on_submit():
        try:
            auth_service.delete_account(current_user._get_current_object(), form.password.data)
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            logout_user()
            flash("Your account and everything in it have been deleted.", "good")
            return redirect(url_for("public.landing"))
    status = _INVALID if form.errors else 200
    return render_template("pages/settings/data.html", form=form, tab="data"), status
