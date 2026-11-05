"""Contacting support, and reading the replies."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from app.extensions import limiter
from app.forms import show_errors, to_id
from app.forms.records import SupportForm
from app.security import rate_limits
from app.security.rbac import login_required
from app.services import support
from app.services.errors import ValidationFailed

bp = Blueprint("support", __name__, url_prefix="/app/support")

_INVALID = 422


@bp.route("", methods=["GET", "POST"])
@login_required
@limiter.limit(
    rate_limits.SUPPORT_PER_ACCOUNT,
    methods=["POST"],
    key_func=rate_limits.current_account,
    deduct_when=rate_limits.form_accepted,
)
def messages() -> Response | str | tuple[str, int]:
    form = SupportForm()
    if form.validate_on_submit():
        try:
            support.send_message(current_user.id, subject=form.subject.data, body=form.body.data)
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Message sent. The reply will show up in your notifications.", "good")
            return redirect(url_for("support.messages"))
    page = support.messages_from(current_user.id, page=to_id(request.args.get("page")) or 1)
    status = _INVALID if form.errors else 200
    return render_template("pages/support.html", form=form, page=page), status
