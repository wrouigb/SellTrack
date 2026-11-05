"""Shared column helpers and the declarative base used by every model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum, Identity, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db

# Flask-SQLAlchemy builds db.Model at runtime, so type checkers are told what it really is.
if TYPE_CHECKING:
    from app.extensions import Base

    Model = Base
else:
    Model = db.Model

#: All money is ``numeric(12, 2)``; see ``app.money``.
Money = Numeric(12, 2)
#: Period totals on a settlement can add up to more than any single amount.
MoneyTotal = Numeric(16, 2)

#: Options for a composite foreign key to ``(user_id, id)``, which makes it impossible for a row
#: to reference another account's person or settlement. Checked at commit, because deleting an
#: account cascades through several tables and the order of those deletes isn't defined.
DEFERRED = "DEFERRED"


def enum_type(enum_class: type, name: str) -> Enum:
    """A native Postgres enum that stores each member's value, not its Python name."""
    return Enum(enum_class, name=name, values_callable=lambda e: [m.value for m in e])


def non_negative(column: str) -> str:
    """SQL check that ``column`` is a real number ≥ 0.

    The ``NaN`` test matters: Postgres sorts ``NaN`` above every number, so
    ``'NaN' >= 0`` is true and a plain range check would let it through.
    """
    return f"{column} >= 0 AND {column} <> 'NaN'"


def positive(column: str) -> str:
    """SQL check that ``column`` is a real number > 0. See :func:`non_negative`."""
    return f"{column} > 0 AND {column} <> 'NaN'"


def primary_key() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin:
    """``created_at`` and ``updated_at``, both UTC. Append-only tables only have ``created_at``."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
