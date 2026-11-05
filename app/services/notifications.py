"""In-app notifications. There's no email, so this is how the app reaches someone."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, insert, literal, select, update

from app.extensions import db
from app.models import AuditLog, Notification, User, UserStatus
from app.security.sessions import is_safe_redirect
from app.services import audit
from app.services.errors import NotFoundError
from app.services.paging import Page, paginate
from app.services.validation import Errors, checked, clean_text

TITLE_MAX = 200
ANNOUNCEMENT_BODY_MAX = 2000
URL_MAX = 500
_ANNOUNCEMENTS_SHOWN = 20


class Kind(StrEnum):
    NEW_SIGN_IN = "security.new_sign_in"
    PASSWORD_CHANGED = "security.password_changed"
    PASSWORD_RESET_BY_ADMIN = "security.password_reset_by_admin"
    RECOVERY_CODE_USED = "security.recovery_code_used"
    RECOVERY_CODES_LOW = "security.recovery_codes_low"
    RECOVERY_CODES_REGENERATED = "security.recovery_codes_regenerated"
    SHARES_NOT_100 = "split.shares_not_100"
    SETTLED = "split.settled"
    SETTLEMENT_UNDONE = "split.settlement_undone"
    SUPPORT_REPLY = "support.reply"
    ANNOUNCEMENT = "announcement"


def notify(
    user_id: int,
    kind: Kind,
    title: str,
    body: str = "",
    url: str | None = None,
    *,
    unless_unread: bool = False,
) -> Notification | None:
    """Add a notification to the current transaction.

    With ``unless_unread``, nothing is added while the same kind is still unread, so a warning
    that keeps being true doesn't pile up.
    """
    if unless_unread and db.session.scalar(
        select(Notification.id)
        .where(
            Notification.user_id == user_id,
            Notification.kind == kind.value,
            Notification.read_at.is_(None),
        )
        .limit(1)
    ):
        return None
    notification = Notification(user_id=user_id, kind=kind.value, title=title, body=body, url=url)
    db.session.add(notification)
    return notification


def dismiss(user_id: int, kind: Kind) -> None:
    """Mark a warning read because it's no longer true. Joins the current transaction."""
    db.session.execute(
        update(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.kind == kind.value,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )


def list_for(
    user_id: int, *, unread_only: bool = False, limit: int = 50, offset: int = 0
) -> Sequence[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc(), Notification.id.desc())
    return db.session.scalars(query.limit(limit).offset(offset)).all()


def page_for(user_id: int, *, unread_only: bool = False, page: int = 1) -> Page[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    return paginate(
        query.order_by(Notification.created_at.desc(), Notification.id.desc()), page=page
    )


def unread_count(user_id: int) -> int:
    return (
        db.session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(user_id: int, notification_id: int) -> Notification:
    notification = db.session.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )
    if notification is None:
        raise NotFoundError("That notification doesn't exist.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
    db.session.commit()
    return notification


def mark_all_read(user_id: int) -> int:
    """Mark everything read and return how many changed."""
    result = db.session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


def announce(actor: User, *, title: str | None, body: str | None, url: str | None = None) -> int:
    """Send a notification to every active account. Returns how many people it went to."""
    errors = Errors()
    clean_title = clean_text(
        errors, "title", title, max_length=TITLE_MAX, required=True, label="The title"
    )
    clean_body = clean_text(
        errors, "body", body, max_length=ANNOUNCEMENT_BODY_MAX, required=True, label="The message"
    )
    clean_url = clean_text(errors, "url", url, max_length=URL_MAX)
    if clean_url and not is_safe_redirect(clean_url):
        errors.add("url", "Use a path on this site, like /help.")
    errors.raise_if_any()

    result = db.session.execute(
        insert(Notification).from_select(
            ["user_id", "kind", "title", "body", "url"],
            select(
                User.id,
                literal(Kind.ANNOUNCEMENT.value),
                literal(checked(clean_title)),
                literal(checked(clean_body)),
                literal(clean_url),
            ).where(User.status == UserStatus.ACTIVE),
        ),
        # An insert's row count is thrown away unless it's asked for.
        execution_options={"preserve_rowcount": True},
    )
    recipients = int(result.rowcount)  # type: ignore[attr-defined]
    audit.record(
        "announcement.sent",
        actor_id=actor.id,
        details={
            "title": clean_title,
            "body": clean_body,
            "url": clean_url,
            "recipients": recipients,
        },
    )
    db.session.commit()
    return recipients


def past_announcements() -> Sequence[AuditLog]:
    """What's been sent, newest first. This comes from the audit log, which is never trimmed."""
    return db.session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "announcement.sent")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(_ANNOUNCEMENTS_SHOWN)
    ).all()
