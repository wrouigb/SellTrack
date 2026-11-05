from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Model, MoneyTotal, TimestampMixin, primary_key


class Settlement(TimestampMixin, Model):
    """A permanent record of a settle-up. Rows in the period point here via ``settlement_id``."""

    __tablename__ = "settlements"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_settlements_user_id_id"),
        Index("ix_settlements_user_recent", "user_id", text("settled_at DESC"), text("id DESC")),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    money_in: Mapped[Decimal] = mapped_column(MoneyTotal, nullable=False)
    upfront_total: Mapped[Decimal] = mapped_column(MoneyTotal, nullable=False)
    profit_to_share: Mapped[Decimal] = mapped_column(MoneyTotal, nullable=False)
    #: Output of ``split_engine.SplitResult.to_snapshot()``.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
