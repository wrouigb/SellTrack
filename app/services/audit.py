"""The audit log of security-sensitive actions.

:func:`record` joins the caller's transaction, so an action and its audit entry
are saved together or not at all.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from flask import has_request_context, request
from sqlalchemy import and_, select
from sqlalchemy.orm import aliased

from app.extensions import db
from app.models import AuditLog, User
from app.observability import current_request_id
from app.services.paging import Page, paginate

_MAX_USER_AGENT = 255

#: Everything that gets recorded, in the words the admin sees. A test checks none are missing.
ACTION_LABELS = {
    "admin.password_reset": "Admin reset a password",
    "admin.user_status_changed": "Admin changed an account's status",
    "announcement.sent": "Announcement sent",
    "auth.account_deleted": "Account deleted",
    "auth.admin_created": "Admin account created",
    "auth.login": "Logged in",
    "auth.login_failed": "Failed login",
    "auth.logout": "Logged out",
    "auth.logout_everywhere": "Logged out everywhere",
    "auth.password_changed": "Password changed",
    "auth.recovery_code_used": "Recovery code used",
    "auth.recovery_codes_regenerated": "New recovery codes created",
    "auth.recovery_failed": "Failed account recovery",
    "auth.registered": "Account created",
    "data.exported": "Data downloaded",
    "flag.changed": "Feature flag changed",
    "flag.deleted": "Feature flag deleted",
    "split.settled": "Settled up",
    "split.settlement_undone": "Settle-up undone",
    "support.closed": "Support message closed",
    "support.reopened": "Support message reopened",
    "support.replied": "Support message answered",
    "system.cleanup": "Nightly cleanup ran",
}

#: What the admin may read of each action's details. Anything not named here stays hidden, so a
#: new detail can't reach the admin pages by accident. The admin isn't meant to see split figures.
_VISIBLE_DETAILS: dict[str, tuple[str, ...]] = {
    "admin.user_status_changed": ("status",),
    "announcement.sent": ("title", "recipients"),
    "flag.changed": ("key", "enabled", "rollout_pct", "allow_user_ids"),
    "flag.deleted": ("key",),
    "split.settled": ("sales", "costs", "payments"),
    "system.cleanup": ("sales", "sign_ins", "notifications"),
}


@dataclass(frozen=True, slots=True)
class AuditFilters:
    action: str = ""
    actor: str = ""
    date_from: date | None = None
    date_to: date | None = None
    #: Which time zone the dates are in. Entries are stored in UTC.
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class AuditRow:
    entry: AuditLog
    label: str
    actor_name: str | None
    target_name: str | None
    details: tuple[tuple[str, str], ...]


def record(
    action: str,
    *,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an entry to the current transaction. Never put a password or code in ``details``."""
    entry = AuditLog(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        ip=client_ip(),
        user_agent=client_user_agent(),
        request_id=current_request_id(),
    )
    db.session.add(entry)
    return entry


def client_ip() -> str | None:
    """The client's address, or ``None`` outside a request or when it isn't a valid IP."""
    if not has_request_context() or not request.remote_addr:
        return None
    try:
        return str(ipaddress.ip_address(request.remote_addr))
    except ValueError:
        return None


def client_user_agent() -> str | None:
    if not has_request_context():
        return None
    agent = request.headers.get("User-Agent", "").replace("\x00", "")
    return agent[:_MAX_USER_AGENT] or None


def label_for(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def visible_details(entry: AuditLog) -> tuple[tuple[str, str], ...]:
    allowed = _VISIBLE_DETAILS.get(entry.action, ())
    return tuple(
        (name, _in_words(entry.details[name])) for name in allowed if name in entry.details
    )


def _in_words(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "None"
    return str(value)


def list_entries(filters: AuditFilters | None = None, *, page: int = 1) -> Page[AuditRow]:
    """Newest first, with the usernames of whoever did it and whoever it was done to."""
    filters = filters or AuditFilters()
    actor = aliased(User)
    target = aliased(User)
    query = (
        select(
            AuditLog,
            actor.username.label("actor_name"),
            target.username.label("target_name"),
        )
        .outerjoin(actor, AuditLog.actor_id == actor.id)
        .outerjoin(target, and_(AuditLog.target_type == "user", AuditLog.target_id == target.id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    if filters.action:
        query = query.where(AuditLog.action == filters.action)
    if filters.actor.strip():
        query = query.where(actor.username == filters.actor.strip())
    zone = ZoneInfo(filters.timezone)
    if filters.date_from:
        query = query.where(AuditLog.created_at >= _start_of(filters.date_from, zone))
    if filters.date_to:
        query = query.where(
            AuditLog.created_at < _start_of(filters.date_to + timedelta(days=1), zone)
        )
    found = paginate(query, page=page, rows=True)
    return Page(
        items=[
            _row(entry, actor_name, target_name) for entry, actor_name, target_name in found.items
        ],
        page=found.page,
        per_page=found.per_page,
        total=found.total,
    )


def recent_for_user(user_id: int, *, limit: int = 10) -> list[AuditRow]:
    """What an account has done, and what's been done to it."""
    entries = db.session.scalars(
        select(AuditLog)
        .where(
            (AuditLog.actor_id == user_id)
            | and_(AuditLog.target_type == "user", AuditLog.target_id == user_id)
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    ).all()
    return [_row(entry, None, None) for entry in entries]


def _row(entry: AuditLog, actor_name: str | None, target_name: str | None) -> AuditRow:
    return AuditRow(
        entry=entry,
        label=label_for(entry.action),
        actor_name=actor_name,
        target_name=target_name,
        details=visible_details(entry),
    )


def _start_of(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
