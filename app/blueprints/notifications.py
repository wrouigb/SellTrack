"""The notifications page and the bell in the top band."""

from __future__ import annotations

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response as BaseResponse

from app.forms import to_id
from app.security.rbac import login_required
from app.security.sessions import is_safe_redirect
from app.services import notifications

bp = Blueprint("notifications", __name__, url_prefix="/app/notifications")

_NOTHING_TO_SWAP = 204


@bp.get("")
@login_required
def list_notifications() -> str:
    unread_only = request.args.get("show") == "unread"
    page = notifications.page_for(
        current_user.id, unread_only=unread_only, page=to_id(request.args.get("page")) or 1
    )
    return render_template(
        "pages/notifications/list.html",
        page=page,
        unread_only=unread_only,
        unread=notifications.unread_count(current_user.id),
    )


@bp.get("/bell")
def bell() -> str | Response:
    """The bell's count, which the page asks for every couple of minutes.

    It isn't ``login_required``, because a redirect would swap the login page into the top band.
    If the session has ended, the bell stays as it is until the next page load.
    """
    if not current_user.is_authenticated:
        return Response(status=_NOTHING_TO_SWAP)
    return render_template(
        "components/bell_count.html", count=notifications.unread_count(current_user.id)
    )


@bp.post("/<int:notification_id>/read")
@login_required
def mark_read(notification_id: int) -> BaseResponse:
    notifications.mark_read(current_user.id, notification_id)
    return _back_to_list()


@bp.post("/<int:notification_id>/open")
@login_required
def open_notification(notification_id: int) -> BaseResponse:
    """Go to what the notification is about, marking it read on the way."""
    notification = notifications.mark_read(current_user.id, notification_id)
    if notification.url and is_safe_redirect(notification.url):
        return redirect(notification.url)
    return _back_to_list()


@bp.post("/read-all")
@login_required
def mark_all_read() -> BaseResponse:
    notifications.mark_all_read(current_user.id)
    flash("All notifications marked as read.", "good")
    return _back_to_list()


def _back_to_list() -> BaseResponse:
    target = request.form.get("next")
    if target and is_safe_redirect(target):
        return redirect(target)
    return redirect(url_for("notifications.list_notifications"))
