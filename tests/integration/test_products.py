"""Products and cost history, including every behaviour of the original app."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import Product, ProductCostChange, Sale
from app.services import products, sales
from app.services.errors import ConflictError, NotFoundError, ValidationFailed
from tests.factories import SaleFactory, UserFactory
from tests.helpers import count_queries

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


TODAY = date(2026, 11, 5)


class TestCreate:
    def test_creates_with_first_cost_history_line(self, user_id):
        product = products.create_product(user_id, name="  Stamp set ", cost="2.50", sell_price="9")
        assert product.name == "Stamp set"
        assert product.cost == Decimal("2.50")
        assert product.sell_price == Decimal("9.00")
        history = products.cost_history(user_id, product.id)
        assert [(h.old_cost, h.new_cost) for h in history] == [(None, Decimal("2.50"))]

    def test_selling_price_is_optional(self, user_id):
        assert products.create_product(user_id, name="A", cost="1").sell_price is None
        assert (
            products.create_product(user_id, name="B", cost="1", sell_price="").sell_price is None
        )

    @pytest.mark.parametrize(
        ("field", "values"),
        [
            ("name", {"name": "", "cost": "1"}),
            ("name", {"name": "   ", "cost": "1"}),
            ("name", {"name": "x" * 101, "cost": "1"}),
            ("cost", {"name": "A", "cost": ""}),
            ("cost", {"name": "A", "cost": "-1"}),
            # Audit: NaN and Infinity passed the original validation and poisoned every total.
            ("cost", {"name": "A", "cost": "NaN"}),
            ("cost", {"name": "A", "cost": "nan"}),
            ("cost", {"name": "A", "cost": "Infinity"}),
            ("cost", {"name": "A", "cost": "-Infinity"}),
            ("cost", {"name": "A", "cost": "1e3"}),
            ("cost", {"name": "A", "cost": "1.005"}),
            ("cost", {"name": "A", "cost": "99999999999"}),
            ("cost", {"name": "A", "cost": 1.5}),  # a float is a programming error, not a price
            ("sell_price", {"name": "A", "cost": "1", "sell_price": "-1"}),
            ("sell_price", {"name": "A", "cost": "1", "sell_price": "NaN"}),
        ],
    )
    def test_rejects_bad_input(self, user_id, field, values):
        with pytest.raises(ValidationFailed) as error:
            products.create_product(user_id, **values)
        assert field in error.value.errors
        assert db.session.scalar(select(func.count()).select_from(Product)) == 0

    def test_reports_every_problem_at_once(self, user_id):
        with pytest.raises(ValidationFailed) as error:
            products.create_product(user_id, name="", cost="abc", sell_price="-1")
        assert set(error.value.errors) == {"name", "cost", "sell_price"}

    def test_names_are_unique_ignoring_case(self, user_id):
        products.create_product(user_id, name="Stamp", cost="1")
        with pytest.raises(ConflictError) as error:
            products.create_product(user_id, name="sTAMP", cost="2")
        assert "already have a product" in error.value.errors["name"]

    def test_different_accounts_can_use_the_same_name(self, user_id):
        other = UserFactory()
        db.session.commit()
        products.create_product(user_id, name="Stamp", cost="1")
        products.create_product(other.id, name="Stamp", cost="1")

    def test_the_session_still_works_after_a_conflict(self, user_id):
        products.create_product(user_id, name="Stamp", cost="1")
        with pytest.raises(ConflictError):
            products.create_product(user_id, name="Stamp", cost="2")
        assert products.create_product(user_id, name="Other", cost="2").id


class TestUpdate:
    def test_cost_change_is_logged_and_only_new_sales_use_it(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2.00")
        old_sale = sales.create_sale(
            user_id, product_id=product.id, sale_date=TODAY, quantity=2, price_paid="10"
        )

        products.update_product(user_id, product.id, name="Stamp", cost="3.00")
        new_sale = sales.create_sale(
            user_id, product_id=product.id, sale_date=TODAY, quantity=2, price_paid="10"
        )

        assert old_sale.cost_each == Decimal("2.00")
        assert old_sale.profit == Decimal("6.00"), "old profit figures never drift"
        assert new_sale.cost_each == Decimal("3.00")
        assert new_sale.profit == Decimal("4.00")
        history = products.cost_history(user_id, product.id)
        assert [(h.old_cost, h.new_cost) for h in history] == [
            (Decimal("2.00"), Decimal("3.00")),
            (None, Decimal("2.00")),
        ]

    def test_saving_the_same_cost_logs_nothing(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2.00")
        products.update_product(user_id, product.id, name="Stamp", cost="2", sell_price="5")
        assert len(products.cost_history(user_id, product.id)) == 1

    def test_rename_flows_to_past_sales_only_for_this_product(self, user_id):
        product = products.create_product(user_id, name="Old name", cost="1")
        other = products.create_product(user_id, name="Other", cost="1")
        mine = sales.create_sale(
            user_id, product_id=product.id, sale_date=TODAY, quantity=1, price_paid="5"
        )
        theirs = sales.create_sale(
            user_id, product_id=other.id, sale_date=TODAY, quantity=1, price_paid="5"
        )
        products.update_product(user_id, product.id, name="New name", cost="1")
        db.session.refresh(mine)
        db.session.refresh(theirs)
        assert mine.product_name == "New name"
        assert theirs.product_name == "Other"

    def test_changing_only_the_case_of_a_name_is_allowed(self, user_id):
        product = products.create_product(user_id, name="stamp", cost="1")
        assert products.update_product(user_id, product.id, name="Stamp", cost="1").name == "Stamp"

    def test_cannot_take_another_products_name(self, user_id):
        products.create_product(user_id, name="First", cost="1")
        second = products.create_product(user_id, name="Second", cost="1")
        with pytest.raises(ConflictError):
            products.update_product(user_id, second.id, name="FIRST", cost="1")
        assert products.get_product(user_id, second.id).name == "Second"

    def test_bad_input_changes_nothing(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="1")
        with pytest.raises(ValidationFailed):
            products.update_product(user_id, product.id, name="Stamp", cost="NaN")
        assert products.get_product(user_id, product.id).cost == Decimal("1.00")


class TestRemoving:
    def test_delete_keeps_past_sales_and_removes_cost_history(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2")
        sale = sales.create_sale(
            user_id, product_id=product.id, sale_date=TODAY, quantity=1, price_paid="5"
        )
        products.delete_product(user_id, product.id)
        db.session.refresh(sale)
        assert sale.product_id is None
        assert sale.product_name == "Stamp"
        assert sale.cost_each == Decimal("2.00")
        assert db.session.scalar(select(func.count()).select_from(ProductCostChange)) == 0

    def test_archive_hides_it_but_keeps_everything(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2")
        sales.create_sale(
            user_id, product_id=product.id, sale_date=TODAY, quantity=1, price_paid="5"
        )
        products.archive_product(user_id, product.id)

        assert products.list_products(user_id) == []
        listed = products.list_products(user_id, include_archived=True)
        assert [row.product.id for row in listed] == [product.id]
        assert listed[0].sale_count == 1
        assert len(products.cost_history(user_id, product.id)) == 1

    def test_archived_product_cannot_be_sold(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2")
        products.archive_product(user_id, product.id)
        with pytest.raises(ValidationFailed) as error:
            sales.create_sale(
                user_id, product_id=product.id, sale_date=TODAY, quantity=1, price_paid="5"
            )
        assert "archived" in error.value.errors["product_id"]

    def test_an_archived_name_can_be_reused_and_blocks_restore_while_taken(self, user_id):
        old = products.create_product(user_id, name="Stamp", cost="2")
        products.archive_product(user_id, old.id)
        new = products.create_product(user_id, name="stamp", cost="3")

        with pytest.raises(ConflictError):
            products.restore_product(user_id, old.id)

        products.archive_product(user_id, new.id)
        assert products.restore_product(user_id, old.id).archived_at is None


class TestList:
    def test_sale_counts_leave_out_deleted_sales(self, user_id):
        product = products.create_product(user_id, name="Stamp", cost="2")
        sold = [
            sales.create_sale(
                user_id, product_id=product.id, sale_date=TODAY, quantity=1, price_paid="5"
            )
            for _ in range(3)
        ]
        sales.delete_sale(user_id, sold[0].id)
        assert products.list_products(user_id)[0].sale_count == 2

    def test_uses_one_query_however_many_products(self, user_id):
        """Audit: the original ran one query per product."""
        for number in range(30):
            product = products.create_product(user_id, name=f"P{number}", cost="1")
            SaleFactory(
                user_id=user_id,
                product_id=product.id,
                product_name=product.name,
                price_paid=Decimal("1.00"),
            )
        db.session.commit()
        with count_queries() as queries:
            rows = products.list_products(user_id)
        assert len(rows) == 30
        assert len(queries) == 1

    def test_margin_percent(self, user_id):
        products.create_product(user_id, name="Good", cost="2", sell_price="8")
        products.create_product(user_id, name="Loss", cost="10", sell_price="8")
        products.create_product(user_id, name="Free", cost="1", sell_price="0")
        products.create_product(user_id, name="Unpriced", cost="1")
        margins = {r.product.name: r.margin_pct for r in products.list_products(user_id)}
        assert margins == {
            "Good": Decimal("75.0"),
            "Loss": Decimal("-25.0"),
            "Free": None,
            "Unpriced": None,
        }

    def test_sorting(self, user_id):
        products.create_product(user_id, name="beta", cost="5", sell_price="10")
        products.create_product(user_id, name="Alpha", cost="1", sell_price="10")
        products.create_product(user_id, name="gamma", cost="3")

        def names(**kwargs):
            return [r.product.name for r in products.list_products(user_id, **kwargs)]

        assert names() == ["Alpha", "beta", "gamma"], "name order ignores case"
        assert names(descending=True) == ["gamma", "beta", "Alpha"]
        assert names(sort="cost") == ["Alpha", "gamma", "beta"]
        assert names(sort="margin", descending=True) == ["Alpha", "beta", "gamma"], "nulls last"

    def test_unknown_sort_is_rejected(self, user_id):
        with pytest.raises(ValidationFailed):
            products.list_products(user_id, sort="password_hash")

    def test_search_is_case_insensitive_and_literal(self, user_id):
        products.create_product(user_id, name="100% Wool", cost="1")
        products.create_product(user_id, name="100 Cotton", cost="1")
        products.create_product(user_id, name="a_b", cost="1")
        products.create_product(user_id, name="axb", cost="1")

        def names(term):
            return {r.product.name for r in products.list_products(user_id, search=term)}

        assert names("wool") == {"100% Wool"}
        assert names("0%") == {"100% Wool"}, "% is matched literally, not as a wildcard"
        assert names("a_b") == {"a_b"}, "_ is matched literally, not as a wildcard"


class TestOwnership:
    def test_another_accounts_product_looks_like_it_does_not_exist(self, user_id):
        other = UserFactory()
        db.session.commit()
        product = products.create_product(other.id, name="Theirs", cost="1")
        calls = [
            lambda: products.get_product(user_id, product.id),
            lambda: products.update_product(user_id, product.id, name="Mine now", cost="0"),
            lambda: products.archive_product(user_id, product.id),
            lambda: products.restore_product(user_id, product.id),
            lambda: products.delete_product(user_id, product.id),
            lambda: products.cost_history(user_id, product.id),
        ]
        for call in calls:
            with pytest.raises(NotFoundError):
                call()
        assert products.get_product(other.id, product.id).name == "Theirs"
        assert products.list_products(user_id) == []

    def test_rename_cannot_touch_another_accounts_sales(self, user_id):
        other = UserFactory()
        db.session.commit()
        mine = products.create_product(user_id, name="Mine", cost="1")
        foreign = SaleFactory(user_id=other.id, product_id=mine.id, product_name="Theirs")
        db.session.commit()
        products.update_product(user_id, mine.id, name="Renamed", cost="1")
        assert db.session.get(Sale, foreign.id).product_name == "Theirs"
