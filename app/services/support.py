"""Support messages. There's no email, so replies reach people as in-app notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select

from app.extensions import db
from app.models import SupportMessage, SupportStatus, User
from app.services import audit
from app.services.errors import ConflictError, NotFoundError
from app.services.notifications import TITLE_MAX, Kind, notify
from app.services.paging import Page, paginate
from app.services.products import like_pattern
from app.services.validation import Errors, checked, clean_text

SUBJECT_MAX = 200
BODY_MAX = 5000


@dataclass(frozen=True, slots=True)
class InboxItem:
    message: SupportMessage
    username: str


def send_message(user_id: int, *, subject: str | None, body: str | None) -> SupportMessage:
    errors = Errors()
    clean_subject = clean_text(
        errors, "subject", subject, max_length=SUBJECT_MAX, required=True, label="The subject"
    )
    clean_body = clean_text(
        errors, "body", body, max_length=BODY_MAX, required=True, label="The message"
    )
    errors.raise_if_any()
    message = SupportMessage(
        user_id=user_id, subject=checked(clean_subject), body=checked(clean_body)
    )
    db.session.add(message)
    db.session.commit()
    return message


def messages_from(user_id: int, *, page: int = 1) -> Page[SupportMessage]:
    return paginate(
        select(SupportMessage)
        .where(SupportMessage.user_id == user_id)
        .order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc()),
        page=page,
    )


def inbox(*, status: SupportStatus | None, search: str = "", page: int = 1) -> Page[InboxItem]:
    """Messages for the admin, oldest first when waiting for a reply, otherwise newest first."""
    query = select(SupportMessage, User.username).join(User, SupportMessage.user_id == User.id)
    if status is not None:
        query = query.where(SupportMessage.status == status)
    if term := search.strip():
        pattern = like_pattern(term)
        query = query.where(
            or_(
                SupportMessage.subject.ilike(pattern, escape="\\"),
                User.username.ilike(pattern, escape="\\"),
            )
        )
    if status is SupportStatus.OPEN:
        query = query.order_by(SupportMessage.created_at, SupportMessage.id)
    else:
        query = query.order_by(SupportMessage.created_at.desc(), SupportMessage.id.desc())
    found = paginate(query, page=page, rows=True)
    items = [InboxItem(message=row[0], username=row[1]) for row in found.items]
    return Page(items=items, page=found.page, per_page=found.per_page, total=found.total)


def open_count() -> int:
    return (
        db.session.scalar(
            select(func.count())
            .select_from(SupportMessage)
            .where(SupportMessage.status == SupportStatus.OPEN)
        )
        or 0
    )


def get_item(message_id: int) -> InboxItem:
    row = db.session.execute(
        select(SupportMessage, User.username)
        .join(User, SupportMessage.user_id == User.id)
        .where(SupportMessage.id == message_id)
    ).one_or_none()
    if row is None:
        raise NotFoundError("That message doesn't exist.")
    return InboxItem(message=row[0], username=row[1])


def reply(actor: User, message_id: int, text: str | None) -> SupportMessage:
    """Answer a message, or change the answer, and let the person know."""
    errors = Errors()
    clean_reply = clean_text(
        errors, "reply", text, max_length=BODY_MAX, required=True, label="The reply"
    )
    errors.raise_if_any()

    message = _lock(message_id)
    if message.status is SupportStatus.CLOSED:
        db.session.rollback()
        raise ConflictError(reply="This message is closed. Reopen it to reply.")
    changed = message.admin_reply is not None
    message.admin_reply = checked(clean_reply)
    message.status = SupportStatus.ANSWERED
    message.replied_at = datetime.now(UTC)
    audit.record(
        "support.replied", actor_id=actor.id, target_type="support_message", target_id=message.id
    )
    title = f"{'Support changed its reply' if changed else 'Support replied'}: {message.subject}"
    notify(
        message.user_id,
        Kind.SUPPORT_REPLY,
        title[:TITLE_MAX],
        message.admin_reply,
        url=f"/app/support#message-{message.id}",
    )
    db.session.commit()
    return message


def close(actor: User, message_id: int) -> None:
    message = _lock(message_id)
    message.status = SupportStatus.CLOSED
    audit.record(
        "support.closed", actor_id=actor.id, target_type="support_message", target_id=message.id
    )
    db.session.commit()


def reopen(actor: User, message_id: int) -> None:
    message = _lock(message_id)
    message.status = SupportStatus.ANSWERED if message.admin_reply else SupportStatus.OPEN
    audit.record(
        "support.reopened", actor_id=actor.id, target_type="support_message", target_id=message.id
    )
    db.session.commit()


def _lock(message_id: int) -> SupportMessage:
    message = db.session.scalar(
        select(SupportMessage)
        .where(SupportMessage.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        db.session.rollback()
        raise NotFoundError("That message doesn't exist.")
    return message
