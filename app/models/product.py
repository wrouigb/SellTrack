from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Model,
    Money,
    TimestampMixin,
    created_at_column,
    non_negative,
    primary_key,
)


class Product(TimestampMixin, Model):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(non_negative("cost"), name="cost_non_negative"),
        CheckConstraint(
            f"sell_price IS NULL OR ({non_negative('sell_price')})",
            name="sell_price_non_negative",
        ),
        # Enforced here rather than only in Python, so two requests at once can't both win.
        Index(
            "uq_products_user_lower_name",
            "user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: What one item costs right now. Each sale keeps its own copy in ``Sale.cost_each``.
    cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    sell_price: Mapped[Decimal | None] = mapped_column(Money)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductCostChange(Model):
    """A diary of every time a product's cost was set or changed. Append-only."""

    __tablename__ = "product_cost_changes"
    __table_args__ = (
        CheckConstraint(
            f"old_cost IS NULL OR ({non_negative('old_cost')})", name="old_cost_non_negative"
        ),
        CheckConstraint(non_negative("new_cost"), name="new_cost_non_negative"),
    )

    id: Mapped[int] = primary_key()
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_cost: Mapped[Decimal | None] = mapped_column(Money)
    new_cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    changed_at: Mapped[datetime] = created_at_column()
