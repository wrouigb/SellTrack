from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

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

from app.models.base import (
    DEFERRED,
    Model,
    Money,
    TimestampMixin,
    enum_type,
    positive,
    primary_key,
)


class CostCategory(StrEnum):
    STOCK = "stock"
    EQUIPMENT = "equipment"
    FEES = "fees"
    PACKAGING = "packaging"
    OTHER = "other"


class UpfrontCost(TimestampMixin, Model):
    """Money someone spent for the business. Whoever paid gets it back in the split."""

    __tablename__ = "upfront_costs"
    __table_args__ = (
        CheckConstraint(positive("amount"), name="amount_positive"),
        ForeignKeyConstraint(
            ["user_id", "paid_by_partner_id"],
            ["partners.user_id", "partners.id"],
            name="fk_upfront_costs_paid_by_partner",
            deferrable=True,
            initially=DEFERRED,
        ),
        ForeignKeyConstraint(
            ["user_id", "settlement_id"],
            ["settlements.user_id", "settlements.id"],
            name="fk_upfront_costs_settlement",
            deferrable=True,
            initially=DEFERRED,
        ),
        Index("ix_upfront_costs_user_date", "user_id", text("cost_date DESC"), text("id DESC")),
        Index("ix_upfront_costs_paid_by_partner_id", "paid_by_partner_id"),
        Index("ix_upfront_costs_settlement_id", "settlement_id"),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[CostCategory] = mapped_column(
        enum_type(CostCategory, "cost_category"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    cost_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_by_partner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    settlement_id: Mapped[int | None] = mapped_column(BigInteger)
