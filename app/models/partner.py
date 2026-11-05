from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Model, TimestampMixin, primary_key


class Partner(TimestampMixin, Model):
    """A person within an account who shares the profit."""

    __tablename__ = "partners"
    __table_args__ = (
        CheckConstraint(
            "share_pct >= 0 AND share_pct <= 100 AND share_pct <> 'NaN'", name="share_pct_range"
        ),
        # Lets other tables point at (user_id, id), so a row can only ever reference a person
        # from its own account.
        UniqueConstraint("user_id", "id", name="uq_partners_user_id_id"),
        Index(
            "uq_partners_user_lower_name",
            "user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index(
            "uq_partners_one_usual_receiver",
            "user_id",
            unique=True,
            postgresql_where=text("receives_money_default AND archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    share_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    receives_money_default: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
