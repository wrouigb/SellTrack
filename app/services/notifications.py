"""In-app notifications. There's no email, so this is how the app reaches someone."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select, update

from app.extensions import db
from app.models import Notification
from app.services.errors import NotFoundError


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


def list_for(
    user_id: int, *, unread_only: bool = False, limit: int = 50, offset: int = 0
) -> Sequence[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc(), Notification.id.desc())
    return db.session.scalars(query.limit(limit).offset(offset)).all()


def unread_count(user_id: int) -> int:
    return (
        db.session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(user_id: int, notification_id: int) -> None:
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


def mark_all_read(user_id: int) -> int:
    """Mark everything read and return how many changed."""
    result = db.session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]
