"""The nightly tidy-up: removes what's past keeping, as the privacy page says it will."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.extensions import db
from app.models import Notification, Sale, SignIn
from app.services import audit
from app.services.auth import RECENT_SIGN_INS

DELETED_SALES_DAYS = 30
SIGN_IN_DAYS = 90
READ_NOTIFICATION_DAYS = 180
#: Any fixed number will do. It only has to be the same in every process.
_ADVISORY_LOCK = 7_301_517


@dataclass(frozen=True, slots=True)
class CleanupResult:
    sales: int
    sign_ins: int
    notifications: int


def run_cleanup(*, now: datetime | None = None) -> CleanupResult | None:
    """Remove old rows and record that it ran. Returns ``None`` if another run has the lock."""
    now = now or datetime.now(UTC)
    # A transaction-level lock, so it can't outlive this function, even if the process dies.
    if not db.session.scalar(select(func.pg_try_advisory_xact_lock(_ADVISORY_LOCK))):
        db.session.rollback()
        return None

    result = CleanupResult(
        sales=_purge_deleted_sales(now - timedelta(days=DELETED_SALES_DAYS)),
        sign_ins=_trim_sign_ins(now - timedelta(days=SIGN_IN_DAYS)),
        notifications=_trim_read_notifications(now - timedelta(days=READ_NOTIFICATION_DAYS)),
    )
    audit.record(
        "system.cleanup",
        details={
            "sales": result.sales,
            "sign_ins": result.sign_ins,
            "notifications": result.notifications,
        },
    )
    db.session.commit()
    return result


def _purge_deleted_sales(cutoff: datetime) -> int:
    # A settled sale can't be deleted, so the second condition should never matter. It's here
    # because the figures of a settle-up are worked out from these rows.
    result = db.session.execute(
        delete(Sale).where(Sale.deleted_at < cutoff, Sale.settlement_id.is_(None))
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def _trim_sign_ins(cutoff: datetime) -> int:
    """Old sign-ins go, except each account's latest few, so its recent sign-ins aren't empty."""
    ranked = select(
        SignIn.id,
        func.row_number()
        .over(partition_by=SignIn.user_id, order_by=(SignIn.created_at.desc(), SignIn.id.desc()))
        .label("position"),
    ).subquery()
    result = db.session.execute(
        delete(SignIn).where(
            SignIn.created_at < cutoff,
            SignIn.id.in_(select(ranked.c.id).where(ranked.c.position > RECENT_SIGN_INS)),
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def _trim_read_notifications(cutoff: datetime) -> int:
    result = db.session.execute(
        delete(Notification).where(Notification.read_at < cutoff, Notification.created_at < cutoff)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
