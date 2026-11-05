from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DEFERRED, Model, Money, TimestampMixin, non_negative, primary_key


class Sale(TimestampMixin, Model):
    __tablename__ = "sales"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(non_negative("price_paid"), name="price_paid_non_negative"),
        CheckConstraint(non_negative("cost_each"), name="cost_each_non_negative"),
        ForeignKeyConstraint(
            ["user_id", "received_by_partner_id"],
            ["partners.user_id", "partners.id"],
            name="fk_sales_received_by_partner",
            deferrable=True,
            initially=DEFERRED,
        ),
        ForeignKeyConstraint(
            ["user_id", "settlement_id"],
            ["settlements.user_id", "settlements.id"],
            name="fk_sales_settlement",
            deferrable=True,
            initially=DEFERRED,
        ),
        Index("ix_sales_user_date", "user_id", text("sale_date DESC"), text("id DESC")),
        # Search is ILIKE '%term%', which only an index on trigrams can help with.
        Index(
            "ix_sales_product_name_trgm",
            "product_name",
            postgresql_using="gin",
            postgresql_ops={"product_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_sales_customer_name_trgm",
            "customer_name",
            postgresql_using="gin",
            postgresql_ops={"customer_name": "gin_trgm_ops"},
        ),
        Index("ix_sales_received_by_partner_id", "received_by_partner_id"),
        Index("ix_sales_settlement_id", "settlement_id"),
        Index(
            "ix_sales_open_period",
            "user_id",
            "received_by_partner_id",
            postgresql_where=text("settlement_id IS NULL AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = primary_key()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    #: Stored on the sale so history survives the product being removed.
    product_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sale_date: Mapped[date] = mapped_column(Date, nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(String(1000))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Total the customer paid for all ``quantity`` items.
    price_paid: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: The product's cost when the sale was made. Never changes, so old profit figures don't drift.
    cost_each: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(
        Money, Computed("quantity * cost_each", persisted=True)
    )
    profit: Mapped[Decimal] = mapped_column(
        Money, Computed("price_paid - quantity * cost_each", persisted=True)
    )
    received_by_partner_id: Mapped[int | None] = mapped_column(BigInteger)
    #: NULL means the sale is in the current, open period.
    settlement_id: Mapped[int | None] = mapped_column(BigInteger)
    #: Soft delete, so a delete can be undone.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
