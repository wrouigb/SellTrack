"""What the admin console shows about accounts.

The admin sees account details and how much each account has done, never what's in it: only
counts of sales, products and split records, not the records themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute

from app.extensions import db
from app.models import (
    Notification,
    Partner,
    PartnerPayment,
    Product,
    Sale,
    Settlement,
    SignIn,
    SupportMessage,
    SupportStatus,
    UpfrontCost,
    User,
    UserRole,
    UserStatus,
)
from app.services.errors import NotFoundError
from app.services.paging import Page, paginate
from app.services.products import like_pattern

_ACTIVE_DAYS = 30
#: The columns the user list can sort by. Anything else in the address falls back to the first.
_USER_SORTS: dict[str, InstrumentedAttribute[Any]] = {
    "joined": User.created_at,
    "username": User.username,
    "last_login": User.last_login_at,
}


@dataclass(frozen=True, slots=True)
class UserFilters:
    search: str = ""
    status: str = ""
    role: str = ""
    sort: str = "joined"
    descending: bool = True


@dataclass(frozen=True, slots=True)
class ActivityCounts:
    products: int
    sales: int
    people: int
    upfront_costs: int
    payments: int
    settle_ups: int
    sign_ins: int
    support_messages: int
    notifications: int


@dataclass(frozen=True, slots=True)
class Overview:
    accounts: int
    active_recently: int
    suspended: int
    open_support: int


def list_users(filters: UserFilters | None = None, *, page: int = 1) -> Page[User]:
    filters = filters or UserFilters()
    query = select(User)
    if term := filters.search.strip():
        query = query.where(User.username.ilike(like_pattern(term), escape="\\"))
    if filters.status in tuple(UserStatus):
        query = query.where(User.status == UserStatus(filters.status))
    if filters.role in tuple(UserRole):
        query = query.where(User.role == UserRole(filters.role))
    column = _USER_SORTS.get(filters.sort, User.created_at)
    order = column.desc() if filters.descending else column.asc()
    return paginate(query.order_by(order.nulls_last(), User.id.desc()), page=page)


def get_user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFoundError("That account doesn't exist.")
    return user


def activity_counts(user_id: int) -> ActivityCounts:
    def count(model: Any, *conditions: ColumnElement[bool]) -> int:
        query = select(func.count()).select_from(model).where(model.user_id == user_id, *conditions)
        return db.session.scalar(query) or 0

    return ActivityCounts(
        products=count(Product, Product.archived_at.is_(None)),
        sales=count(Sale, Sale.deleted_at.is_(None)),
        people=count(Partner, Partner.archived_at.is_(None)),
        upfront_costs=count(UpfrontCost),
        payments=count(PartnerPayment),
        settle_ups=count(Settlement),
        sign_ins=count(SignIn),
        support_messages=count(SupportMessage),
        notifications=count(Notification),
    )


def overview(*, now: datetime | None = None) -> Overview:
    since = (now or datetime.now(UTC)) - timedelta(days=_ACTIVE_DAYS)
    accounts, active, suspended = db.session.execute(
        select(
            func.count(),
            func.count().filter(User.last_login_at >= since),
            func.count().filter(User.status == UserStatus.SUSPENDED),
        ).select_from(User)
    ).one()
    open_support = db.session.scalar(
        select(func.count())
        .select_from(SupportMessage)
        .where(SupportMessage.status == SupportStatus.OPEN)
    )
    return Overview(
        accounts=accounts,
        active_recently=active,
        suspended=suspended,
        open_support=open_support or 0,
    )
