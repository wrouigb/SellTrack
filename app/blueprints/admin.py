"""The admin console.

Every route here is admin-only because of the check on the whole blueprint, so a new route can't
be left open by forgetting a decorator. A test visits them all to be sure.
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from app.forms import show_errors, to_id
from app.forms.admin import (
    AnnouncementForm,
    AuditFilterForm,
    EditFlagForm,
    NewFlagForm,
    SupportFilterForm,
    SupportReplyForm,
    UserFilterForm,
)
from app.models import SupportStatus, UserStatus
from app.security.rbac import only_admins
from app.security.sessions import is_safe_redirect
from app.services import admin, audit, auth, flags, notifications, support, system
from app.services.admin import UserFilters
from app.services.audit import AuditFilters
from app.services.errors import ValidationFailed

bp = Blueprint("admin", __name__, url_prefix="/admin")
bp.before_request(only_admins)

_INVALID = 422
_MAX_BULK = 100
_EARLIEST_FILTER_YEAR = 2000
_LATEST_FILTER_YEAR = 2100


@bp.context_processor
def _everywhere() -> dict[str, int]:
    # An error page shows none of this, and the database may be what failed.
    return {} if g.get("rendering_error") else {"open_support": support.open_count()}


@bp.get("")
def overview() -> str:
    return render_template(
        "pages/admin/overview.html",
        tab="overview",
        figures=admin.overview(),
        health=system.health(),
    )


# -- Accounts ------------------------------------------------------------------------------------


@bp.get("/users")
def users() -> str:
    form = UserFilterForm(request.args)
    filters = UserFilters(
        search=form.q.data or "",
        status=form.status.data or "",
        role=form.role.data or "",
        sort=request.args.get("sort", "joined"),
        descending=request.args.get("dir", "desc") != "asc",
    )
    return render_template(
        "pages/admin/users.html",
        tab="users",
        form=form,
        filters=filters,
        page=admin.list_users(filters, page=_page_number()),
        filtered=bool(filters.search or filters.status or filters.role),
    )


@bp.post("/users/bulk-suspend")
def bulk_suspend() -> Response:
    chosen = [to_id(value) for value in request.form.getlist("ids")[:_MAX_BULK]]
    suspended = auth.bulk_suspend(current_user, [user_id for user_id in chosen if user_id])
    if suspended:
        flash(f"Suspended {suspended} account{'' if suspended == 1 else 's'}.", "good")
    else:
        flash("Nothing to suspend. You can't suspend your own account.", "error")
    return _back_to(url_for("admin.users"))


@bp.get("/users/<int:user_id>")
def user_detail(user_id: int) -> str:
    return render_template(
        "pages/admin/user_detail.html",
        tab="users",
        user=admin.get_user(user_id),
        counts=admin.activity_counts(user_id),
        activity=audit.recent_for_user(user_id),
    )


@bp.post("/users/<int:user_id>/suspend")
def suspend_user(user_id: int) -> Response:
    return _set_status(user_id, UserStatus.SUSPENDED, "Account suspended.")


@bp.post("/users/<int:user_id>/reactivate")
def reactivate_user(user_id: int) -> Response:
    return _set_status(user_id, UserStatus.ACTIVE, "Account reactivated.")


def _set_status(user_id: int, status: UserStatus, done: str) -> Response:
    try:
        auth.set_user_status(current_user, user_id, status)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
    else:
        flash(done, "good")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@bp.post("/users/<int:user_id>/reset-password")
def reset_password(user_id: int) -> Response | str:
    """Show a temporary password, this one time.

    It's rendered here rather than flashed, because flashed messages travel in the session cookie.
    """
    try:
        temporary = auth.admin_reset_password(current_user, user_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    return render_template(
        "pages/admin/temporary_password.html",
        tab="users",
        user=admin.get_user(user_id),
        temporary=temporary,
    )


# -- Support -------------------------------------------------------------------------------------


@bp.get("/support")
def support_inbox() -> str:
    form = SupportFilterForm(request.args)
    chosen = form.status.data or "open"
    return render_template(
        "pages/admin/support.html",
        tab="support",
        form=form,
        chosen=chosen,
        page=support.inbox(
            status=None if chosen not in tuple(SupportStatus) else SupportStatus(chosen),
            search=form.q.data or "",
            page=_page_number(),
        ),
    )


@bp.route("/support/<int:message_id>", methods=["GET", "POST"])
def support_message(message_id: int) -> Response | str | tuple[str, int]:
    item = support.get_item(message_id)
    form = SupportReplyForm()
    if form.validate_on_submit():
        try:
            support.reply(current_user, message_id, form.reply.data)
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"Reply sent to {item.username}.", "good")
            return redirect(url_for("admin.support_inbox"))
    elif request.method == "GET":
        form.reply.data = item.message.admin_reply
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/admin/support_message.html", tab="support", item=item, form=form
    ), status


@bp.post("/support/<int:message_id>/close")
def close_support_message(message_id: int) -> Response:
    support.close(current_user, message_id)
    flash("Message closed.", "good")
    return redirect(url_for("admin.support_inbox"))


@bp.post("/support/<int:message_id>/reopen")
def reopen_support_message(message_id: int) -> Response:
    support.reopen(current_user, message_id)
    flash("Message reopened.", "good")
    return redirect(url_for("admin.support_message", message_id=message_id))


# -- Announcements -------------------------------------------------------------------------------


@bp.route("/announcements", methods=["GET", "POST"])
def announcements() -> Response | str | tuple[str, int]:
    form = AnnouncementForm()
    if form.validate_on_submit():
        try:
            sent_to = notifications.announce(
                current_user, title=form.title.data, body=form.body.data, url=form.url.data
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"Announcement sent to {sent_to} account{'' if sent_to == 1 else 's'}.", "good")
            return redirect(url_for("admin.announcements"))
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/admin/announcements.html",
        tab="announcements",
        form=form,
        past=notifications.past_announcements(),
    ), status


# -- Audit log -----------------------------------------------------------------------------------


@bp.get("/audit")
def audit_log() -> str:
    form = AuditFilterForm(request.args)
    filters = AuditFilters(
        action=form.action.data or "",
        actor=form.actor.data or "",
        date_from=_filter_date(form.date_from.data),
        date_to=_filter_date(form.date_to.data),
        timezone=current_user.timezone,
    )
    return render_template(
        "pages/admin/audit.html",
        tab="audit",
        form=form,
        page=audit.list_entries(filters, page=_page_number()),
    )


# -- Feature flags -------------------------------------------------------------------------------


@bp.route("/flags", methods=["GET", "POST"])
def feature_flags() -> Response | str | tuple[str, int]:
    form = NewFlagForm()
    if form.validate_on_submit():
        try:
            flag = flags.create_flag(
                form.key.data or "",
                actor_id=current_user.id,
                description=form.description.data or "",
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"Flag {flag.key} added, switched off.", "good")
            return redirect(url_for("admin.edit_flag", flag_id=flag.id))
    status = _INVALID if form.errors else 200
    rows = [(flag, flags.allowlist_names(flag)) for flag in flags.list_flags()]
    return render_template("pages/admin/flags.html", tab="flags", form=form, rows=rows), status


@bp.route("/flags/<int:flag_id>", methods=["GET", "POST"])
def edit_flag(flag_id: int) -> Response | str | tuple[str, int]:
    flag = flags.get_flag(flag_id)
    form = EditFlagForm()
    if form.validate_on_submit():
        try:
            flags.set_flag(
                flag.key,
                actor_id=current_user.id,
                enabled=form.enabled.data,
                rollout_pct=_whole_percent(form.rollout_pct.data),
                allow_user_ids=flags.parse_allowlist(form.allow_users.data),
                description=form.description.data or "",
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"Flag {flag.key} saved.", "good")
            return redirect(url_for("admin.feature_flags"))
    elif request.method == "GET":
        form.description.data = flag.description
        form.enabled.data = flag.enabled
        form.rollout_pct.data = str(flag.rollout_pct)
        form.allow_users.data = ", ".join(flags.allowlist_names(flag))
    status = _INVALID if form.errors else 200
    return render_template("pages/admin/flag_edit.html", tab="flags", flag=flag, form=form), status


@bp.post("/flags/<int:flag_id>/delete")
def delete_flag(flag_id: int) -> Response:
    flag = flags.get_flag(flag_id)
    key = flag.key
    flags.delete_flag(key, actor_id=current_user.id)
    flash(f"Flag {key} deleted.", "good")
    return redirect(url_for("admin.feature_flags"))


def _whole_percent(value: str | None) -> int:
    text = (value or "").strip()
    if not text.isascii() or not text.isdecimal() or len(text) > len("100"):
        raise ValidationFailed(rollout_pct="Enter a percentage from 0 to 100.")
    return int(text)


# -- System --------------------------------------------------------------------------------------


@bp.get("/system")
def system_health() -> str:
    return render_template("pages/admin/system.html", tab="system", health=system.health())


# -- Helpers -------------------------------------------------------------------------------------


def _page_number() -> int:
    return to_id(request.args.get("page")) or 1


def _filter_date(value: str | None) -> date | None:
    """A date from the address. Anything odd is ignored, including years a calendar can't reach."""
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    if day is None or not _EARLIEST_FILTER_YEAR <= day.year <= _LATEST_FILTER_YEAR:
        return None
    return day


def _back_to(fallback: str) -> Response:
    target = request.form.get("next")
    return redirect(target if target and is_safe_redirect(target) else fallback)
