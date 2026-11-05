"""Test data builders.

These insert rows directly, so a test can set up a situation (say, a settled sale) without
going through the services it isn't testing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.extensions import db
from app.models import (
    CostCategory,
    Partner,
    PartnerPayment,
    Product,
    Sale,
    Settlement,
    UpfrontCost,
    User,
)
from app.security import passwords

PASSWORD = "correct-horse-battery"


class _Base(SQLAlchemyModelFactory):
    class Meta:
        abstract = True
        sqlalchemy_session = db.session
        sqlalchemy_session_persistence = "flush"


class UserFactory(_Base):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    password_hash = factory.LazyFunction(lambda: passwords.hash_password(PASSWORD))


class ProductFactory(_Base):
    class Meta:
        model = Product

    user_id = factory.LazyFunction(lambda: UserFactory().id)
    name = factory.Sequence(lambda n: f"Product {n}")
    cost = Decimal("2.00")
    sell_price = Decimal("5.00")


class PartnerFactory(_Base):
    class Meta:
        model = Partner

    user_id = factory.LazyFunction(lambda: UserFactory().id)
    name = factory.Sequence(lambda n: f"Person {n}")
    share_pct = Decimal("50.00")


class SaleFactory(_Base):
    class Meta:
        model = Sale

    user_id = factory.LazyFunction(lambda: UserFactory().id)
    product_name = "Thing"
    sale_date = date(2026, 11, 1)
    quantity = 1
    price_paid = Decimal("10.00")
    cost_each = Decimal("2.00")


class UpfrontCostFactory(_Base):
    class Meta:
        model = UpfrontCost

    description = "Stock"
    category = CostCategory.STOCK
    amount = Decimal("10.00")
    cost_date = date(2026, 11, 1)


class PaymentFactory(_Base):
    class Meta:
        model = PartnerPayment

    amount = Decimal("5.00")
    paid_on = date(2026, 11, 1)


class SettlementFactory(_Base):
    class Meta:
        model = Settlement

    period_start = date(2026, 11, 1)
    period_end = date(2026, 11, 1)
    money_in = Decimal("0.00")
    upfront_total = Decimal("0.00")
    profit_to_share = Decimal("0.00")
    snapshot = factory.LazyFunction(lambda: {"version": 1, "people": [], "transfers": []})
