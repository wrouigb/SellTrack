"""The billing hook: everything's unlimited today, and it's ready for when that changes."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.services import auth, entitlements, products, sales, split
from app.services.entitlements import Entitlements, Limit
from app.services.errors import ValidationFailed
from tests.conftest import login
from tests.factories import PASSWORD, UserFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


@pytest.fixture
def plan(monkeypatch):
    def set_plan(**limits):
        chosen = Entitlements(limits={Limit(name): value for name, value in limits.items()})
        monkeypatch.setattr(entitlements, "entitlements", lambda _user_id: chosen)

    return set_plan


def test_everything_is_unlimited_today(user_id):
    assert entitlements.entitlements(user_id) == entitlements.UNLIMITED
    for limit in Limit:
        entitlements.check(user_id, limit)


def test_products_can_be_limited(user_id, plan):
    plan(products=1)
    first = products.create_product(user_id, name="One", cost="1")
    with pytest.raises(ValidationFailed, match="Your plan allows 1 products"):
        products.create_product(user_id, name="Two", cost="1")
    products.archive_product(user_id, first.id)
    products.create_product(user_id, name="Three", cost="1")


def test_people_can_be_limited(user_id, plan):
    plan(people=1)
    split.add_person(user_id, name="Alex", share_pct="100")
    with pytest.raises(ValidationFailed):
        split.add_person(user_id, name="Sam", share_pct="0")


def test_sales_can_be_limited(user_id, plan):
    product = products.create_product(user_id, name="One", cost="1")
    person = split.add_person(user_id, name="Alex", share_pct="100")

    def sell():
        return sales.create_sale(
            user_id,
            product_id=product.id,
            sale_date="2026-11-05",
            quantity=1,
            price_paid="1",
            received_by_partner_id=person.id,
        )

    plan(sales=1)
    first = sell()
    with pytest.raises(ValidationFailed):
        sell()
    sales.delete_sale(user_id, first.id)
    sell()


def test_limits_apply_per_account(user_id, plan):
    plan(products=1)
    products.create_product(user_id, name="One", cost="1")
    other = UserFactory()
    db.session.commit()
    products.create_product(other.id, name="One", cost="1")


def test_the_page_says_why(app, client, plan):
    plan(products=0)
    with app.app_context():
        auth.register("alex", PASSWORD)
    login(client, "alex", PASSWORD)
    response = client.post(
        "/app/products", data={"name": "One", "cost": "1", "sell_price": ""}, follow_redirects=True
    )
    assert "Your plan allows 0 products" in response.text
