from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DEFERRED, Model, Money, TimestampMixin, positive, primary_key


class PartnerPayment(TimestampMixin, Model):
    """Money one person gave another, e.g. "Sam paid Alex $20"."""

    __tablename__ = "partner_payments"
    __table_args__ = (
        CheckConstraint(positive("amount"), name="amount_positive"),
        CheckConstraint("from_partner_id <> to_partner_id", name="different_people"),
        ForeignKeyConstraint(
            ["user_id", "from_partner_id"],
            ["partners.user_id", "partners.id"],
            name="fk_partner_payments_from_partner",
            deferrable=True,
            initially=DEFERRED,
        ),
        ForeignKeyConstraint(
            ["user_id", "to_partner_id"],
            ["partners.user_id", "partners.id"],
            name="fk_partner_payments_to_partner",
            deferrable=True,
            initially=DEFERRED,
        ),
        ForeignKeyConstraint(
            ["user_id", "settlement_id"],
            ["settlements.user_id", "settlements.id"],
            name="fk_partner_payments_settlement",
            deferrable=True,
            initially=DEFERRED,
        ),
        Index("ix_partner_payments_user_date", "user_id", text("paid_on DESC"), text("id DESC")),
        Index("ix_partner_payments_from_partner_id", "from_partner_id"),
        Index("ix_partner_payments_to_partner_id", "to_partner_id"),
        Index("ix_partner_payments_settlement_id", "settlement_id"),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    from_partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    settlement_id: Mapped[int | None] = mapped_column(BigInteger)
