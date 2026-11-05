"""What an account is allowed. This is where billing would plug in: today, everything's unlimited.

Services call :func:`check` wherever a paid plan might one day set a limit, so adding plans later
means changing :func:`entitlements` and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select

from app.extensions import db
from app.models import Partner, Product, Sale
from app.services.errors import ValidationFailed


class Limit(StrEnum):
    PRODUCTS = "products"
    PEOPLE = "people"
    SALES = "sales"


@dataclass(frozen=True, slots=True)
class Entitlements:
    #: The most of each thing an account may have. Anything missing is unlimited.
    limits: Mapping[Limit, int] = field(default_factory=dict)


UNLIMITED = Entitlements()

_COUNTED: dict[Limit, tuple[Any, Any]] = {
    Limit.PRODUCTS: (Product, Product.archived_at.is_(None)),
    Limit.PEOPLE: (Partner, Partner.archived_at.is_(None)),
    Limit.SALES: (Sale, Sale.deleted_at.is_(None)),
}


def entitlements(user_id: int) -> Entitlements:
    return UNLIMITED


def check(user_id: int, limit: Limit) -> None:
    """Raise ``ValidationFailed`` if the account has no room for another of these."""
    allowed = entitlements(user_id).limits.get(limit)
    if allowed is None:
        return
    model, still_counts = _COUNTED[limit]
    used = db.session.scalar(
        select(func.count()).select_from(model).where(model.user_id == user_id, still_counts)
    )
    if (used or 0) >= allowed:
        db.session.rollback()
        raise ValidationFailed(plan=f"Your plan allows {allowed} {limit.value}.")
