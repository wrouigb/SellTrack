"""Sales, including every behaviour of the original app and the audit's bugs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import Sale
from app.services import products, sales, split
from app.services.errors import LockedError, NotFoundError, ValidationFailed
from app.services.sales import SaleFilters
from tests.factories import SaleFactory, UserFactory

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 5)


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


@pytest.fixture
def product(user_id):
    return products.create_product(user_id, name="Stamp", cost="2.00", sell_price="5.00")


def make_sale(user_id, product, **values):
    values = {"sale_date": TODAY, "quantity": 1, "price_paid": "10", **values}
    return sales.create_sale(user_id, product_id=product.id, **values)


class TestCreate:
    def test_copies_cost_and_name_and_works_out_profit(self, user_id, product):
        sale = make_sale(user_id, product, quantity=3, price_paid="20", customer_name=" Pat ")
        assert sale.user_id == user_id
        assert sale.product_name == "Stamp"
        assert sale.cost_each == Decimal("2.00")
        assert sale.total_cost == Decimal("6.00")
        assert sale.profit == Decimal("14.00")
        assert sale.customer_name == "Pat"

    def test_money_is_exact(self, user_id):
        """Audit: with floats, 0.7 × 3 was 2.0999999999999996."""
        product = products.create_product(user_id, name="Cheap", cost="0.70")
        sale = make_sale(user_id, product, quantity=3, price_paid="2.10")
        assert sale.total_cost == Decimal("2.10")
        assert sale.profit == Decimal("0.00")

    def test_a_loss_is_allowed(self, user_id, product):
        assert make_sale(user_id, product, quantity=5, price_paid="1").profit == Decimal("-9.00")

    def test_a_free_item_is_allowed(self, user_id, product):
        assert make_sale(user_id, product, price_paid="0").price_paid == Decimal("0.00")

    def test_accepts_formatted_amounts_and_dates_as_text(self, user_id, product):
        sale = make_sale(
            user_id, product, price_paid="$1,234.50", sale_date="2026-11-05", quantity="2"
        )
        assert sale.price_paid == Decimal("1234.50")
        assert sale.sale_date == TODAY
        assert sale.quantity == 2

    @pytest.mark.parametrize(
        ("field", "values"),
        [
            ("price_paid", {"price_paid": "NaN"}),
            ("price_paid", {"price_paid": "Infinity"}),
            ("price_paid", {"price_paid": "-Infinity"}),
            ("price_paid", {"price_paid": "1e3"}),
            ("price_paid", {"price_paid": "-1"}),
            ("price_paid", {"price_paid": ""}),
            ("price_paid", {"price_paid": None}),
            ("price_paid", {"price_paid": "١٢"}),  # Arabic-Indic digits
            ("price_paid", {"price_paid": "10000000000"}),
            ("price_paid", {"price_paid": 10.5}),
            ("quantity", {"quantity": 0}),
            ("quantity", {"quantity": -1}),
            ("quantity", {"quantity": "abc"}),
            ("quantity", {"quantity": "1.5"}),
            ("quantity", {"quantity": "1e3"}),
            ("quantity", {"quantity": "٣"}),
            ("quantity", {"quantity": None}),
            ("quantity", {"quantity": True}),
            ("quantity", {"quantity": 1_000_001}),
            ("sale_date", {"sale_date": None}),
            ("sale_date", {"sale_date": ""}),
            ("sale_date", {"sale_date": "not a date"}),
            ("sale_date", {"sale_date": "2026-13-01"}),
            ("sale_date", {"sale_date": "05/11/2026"}),
            ("sale_date", {"sale_date": date(1999, 12, 31)}),
            ("sale_date", {"sale_date": date(2100, 1, 1)}),
            ("customer_name", {"customer_name": "x" * 101}),
            ("notes", {"notes": "x" * 1001}),
        ],
    )
    def test_rejects_bad_input(self, user_id, product, field, values):
        """Audit: long input was a 500 on Postgres, and NaN poisoned every total."""
        with pytest.raises(ValidationFailed) as error:
            make_sale(user_id, product, **values)
        assert field in error.value.errors
        assert db.session.scalar(select(func.count()).select_from(Sale)) == 0

    def test_null_bytes_in_text_do_not_crash_postgres(self, user_id, product):
        sale = make_sale(user_id, product, customer_name="Pa\x00t")
        assert sale.customer_name == "Pat"

    def test_rejects_totals_too_large_to_store(self, user_id):
        product = products.create_product(user_id, name="Yacht", cost="9999999999.99")
        with pytest.raises(ValidationFailed) as error:
            make_sale(user_id, product, quantity=2, price_paid="1")
        assert "too large" in error.value.errors["quantity"]

    def test_needs_a_product_of_your_own(self, user_id):
        other = UserFactory()
        db.session.commit()
        theirs = products.create_product(other.id, name="Theirs", cost="1")
        with pytest.raises(ValidationFailed) as error:
            sales.create_sale(
                user_id, product_id=theirs.id, sale_date=TODAY, quantity=1, price_paid="1"
            )
        assert "product_id" in error.value.errors

    def test_product_that_does_not_exist(self, user_id):
        with pytest.raises(ValidationFailed):
            sales.create_sale(user_id, product_id=999, sale_date=TODAY, quantity=1, price_paid="1")


class TestReceivedBy:
    def test_uses_the_usual_receiver_and_stores_it(self, user_id, product):
        alex = split.add_person(user_id, name="Alex", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        sale = make_sale(user_id, product)
        assert sale.received_by_partner_id == alex.id

        split.set_usual_receiver(user_id, sam.id)
        assert make_sale(user_id, product).received_by_partner_id == sam.id
        db.session.refresh(sale)
        assert sale.received_by_partner_id == alex.id, "changing the default rewrites nothing"

    def test_explicit_receiver(self, user_id, product):
        split.add_person(user_id, name="Alex", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        sale = make_sale(user_id, product, received_by_partner_id=sam.id)
        assert sale.received_by_partner_id == sam.id

    def test_nobody_to_receive_it_yet(self, user_id, product):
        assert make_sale(user_id, product).received_by_partner_id is None

    def test_cannot_use_someone_from_another_account(self, user_id, product):
        other = UserFactory()
        db.session.commit()
        theirs = split.add_person(other.id, name="Theirs", share_pct="100")
        with pytest.raises(ValidationFailed) as error:
            make_sale(user_id, product, received_by_partner_id=theirs.id)
        assert "received_by_partner_id" in error.value.errors

    def test_cannot_use_an_archived_person(self, user_id, product):
        alex = split.add_person(user_id, name="Alex", share_pct="100")
        split.archive_person(user_id, alex.id)
        with pytest.raises(ValidationFailed):
            make_sale(user_id, product, received_by_partner_id=alex.id)


class TestUpdate:
    def _update(self, user_id, sale, **values):
        current = {
            "sale_date": sale.sale_date,
            "quantity": sale.quantity,
            "price_paid": sale.price_paid,
            "customer_name": sale.customer_name,
            "notes": sale.notes,
            "received_by_partner_id": sale.received_by_partner_id,
        }
        return sales.update_sale(user_id, sale.id, **{**current, **values})

    def test_changes_date_customer_quantity_and_price(self, user_id, product):
        sale = make_sale(user_id, product)
        updated = self._update(
            user_id,
            sale,
            sale_date="2026-10-01",
            customer_name="Pat",
            quantity=2,
            price_paid="30",
        )
        assert updated.sale_date == date(2026, 10, 1)
        assert updated.customer_name == "Pat"
        assert updated.quantity == 2
        assert updated.price_paid == Decimal("30.00")
        assert updated.total_cost == Decimal("4.00"), "calculated columns follow"
        assert updated.profit == Decimal("26.00")

    def test_product_and_cost_stay_locked(self, user_id, product):
        sale = make_sale(user_id, product)
        products.update_product(user_id, product.id, name="Stamp", cost="9.00")
        updated = self._update(user_id, sale, quantity=2)
        assert updated.cost_each == Decimal("2.00")
        assert updated.product_name == "Stamp"
        assert updated.product_id == product.id

    def test_bad_input_changes_nothing(self, user_id, product):
        sale = make_sale(user_id, product)
        with pytest.raises(ValidationFailed):
            self._update(user_id, sale, price_paid="NaN")
        db.session.refresh(sale)
        assert sale.price_paid == Decimal("10.00")

    def test_rejects_quantity_that_overflows_a_money_column(self, user_id):
        product = products.create_product(user_id, name="Big", cost="9999999.00")
        sale = make_sale(user_id, product)
        with pytest.raises(ValidationFailed):
            self._update(user_id, sale, quantity=1_000_000)

    def test_keeps_an_archived_receiver_when_they_are_unchanged(self, user_id, product):
        alex = split.add_person(user_id, name="Alex", share_pct="50")
        split.add_person(user_id, name="Sam", share_pct="50")
        sale = make_sale(user_id, product, received_by_partner_id=alex.id)
        split.settle_up(user_id)
        split.archive_person(user_id, alex.id)
        updated = self._update(user_id, sale, customer_name="Pat")
        assert updated.received_by_partner_id == alex.id
        assert updated.customer_name == "Pat"


class TestSettledSales:
    """Sales in a settled period have their money locked until it's undone (plan section 3.1)."""

    @pytest.fixture
    def settled(self, user_id, product):
        split.add_person(user_id, name="Alex", share_pct="100")
        sale = make_sale(user_id, product)
        split.settle_up(user_id)
        return sale

    def _update(self, user_id, sale, **values):
        current = {
            "sale_date": sale.sale_date,
            "quantity": sale.quantity,
            "price_paid": sale.price_paid,
            "customer_name": sale.customer_name,
            "received_by_partner_id": sale.received_by_partner_id,
        }
        return sales.update_sale(user_id, sale.id, **{**current, **values})

    @pytest.mark.parametrize("change", [{"quantity": 2}, {"price_paid": "99"}])
    def test_money_fields_are_locked(self, user_id, settled, change):
        with pytest.raises(LockedError):
            self._update(user_id, settled, **change)
        db.session.refresh(settled)
        assert settled.quantity == 1
        assert settled.price_paid == Decimal("10.00")

    def test_who_received_it_is_locked(self, user_id, settled):
        sam = split.add_person(user_id, name="Sam", share_pct="0")
        with pytest.raises(LockedError):
            self._update(user_id, settled, received_by_partner_id=sam.id)

    def test_other_fields_can_still_change(self, user_id, settled):
        updated = self._update(user_id, settled, customer_name="Pat", sale_date="2026-10-01")
        assert updated.customer_name == "Pat"
        assert updated.sale_date == date(2026, 10, 1)

    def test_saving_identical_money_is_fine(self, user_id, settled):
        self._update(user_id, settled, price_paid="10.00")

    def test_cannot_be_deleted(self, user_id, settled):
        with pytest.raises(LockedError):
            sales.delete_sale(user_id, settled.id)
        assert sales.get_sale(user_id, settled.id).deleted_at is None

    def test_bulk_delete_skips_and_reports_them(self, user_id, product, settled):
        open_sale = make_sale(user_id, product)
        result = sales.delete_sales(user_id, [settled.id, open_sale.id])
        assert result.deleted_ids == (open_sale.id,)
        assert result.locked_ids == (settled.id,)

    def test_undo_unlocks_them(self, user_id, settled):
        split.undo_last_settlement(user_id)
        assert self._update(user_id, settled, quantity=2).quantity == 2


class TestDelete:
    def test_delete_hides_it_and_restore_brings_it_back(self, user_id, product):
        sale = make_sale(user_id, product)
        sales.delete_sale(user_id, sale.id)

        with pytest.raises(NotFoundError):
            sales.get_sale(user_id, sale.id)
        assert sales.list_sales(user_id).totals.count == 0

        assert sales.restore_sales(user_id, [sale.id]) == 1
        assert sales.get_sale(user_id, sale.id).id == sale.id
        assert sales.list_sales(user_id).totals.count == 1

    def test_deleting_twice_is_not_found(self, user_id, product):
        sale = make_sale(user_id, product)
        sales.delete_sale(user_id, sale.id)
        with pytest.raises(NotFoundError):
            sales.delete_sale(user_id, sale.id)

    def test_bulk_delete_and_undo(self, user_id, product):
        ids = [make_sale(user_id, product).id for _ in range(3)]
        assert set(sales.delete_sales(user_id, ids[:2]).deleted_ids) == set(ids[:2])
        assert sales.list_sales(user_id).totals.count == 1
        assert sales.restore_sales(user_id, ids[:2]) == 2
        assert sales.list_sales(user_id).totals.count == 3

    def test_restoring_something_not_deleted_does_nothing(self, user_id, product):
        assert sales.restore_sales(user_id, [make_sale(user_id, product).id]) == 0


class TestList:
    def _fill(self, user_id):
        rows = [
            ("Ink pad", "Pat Smith", date(2026, 1, 10), 1, "10.00", "1.00"),
            ("Stamp", "pat jones", date(2026, 2, 10), 2, "30.00", "5.00"),
            ("Stamp", "Sam", date(2026, 3, 10), 3, "5.00", "5.00"),
            ("100% Wool", None, date(2026, 4, 10), 1, "7.00", "1.00"),
        ]
        for name, customer, day, quantity, price, cost in rows:
            SaleFactory(
                user_id=user_id,
                product_name=name,
                customer_name=customer,
                sale_date=day,
                quantity=quantity,
                price_paid=Decimal(price),
                cost_each=Decimal(cost),
            )
        db.session.commit()

    def _names(self, user_id, **filters):
        result = sales.list_sales(user_id, SaleFilters(**filters))
        return [(s.product_name, s.customer_name) for s in result.items]

    def test_newest_first_by_default(self, user_id):
        self._fill(user_id)
        result = sales.list_sales(user_id)
        assert [s.sale_date.month for s in result.items] == [4, 3, 2, 1]

    def test_totals_are_for_what_matched(self, user_id):
        """The original showed totals for just the rows on screen."""
        self._fill(user_id)
        totals = sales.list_sales(user_id, SaleFilters(search="stamp")).totals
        assert totals.count == 2
        assert totals.items_sold == 5
        assert totals.money_in == Decimal("35.00")
        assert totals.total_cost == Decimal("25.00")
        assert totals.profit == Decimal("10.00")

    def test_no_matches(self, user_id):
        totals = sales.list_sales(user_id, SaleFilters(search="nothing")).totals
        assert totals.count == 0
        assert totals.money_in == Decimal("0")

    def test_search_finds_customer_or_product_ignoring_case(self, user_id):
        """Audit: Postgres LIKE is case-sensitive, unlike the SQLite the original ran on."""
        self._fill(user_id)
        assert len(self._names(user_id, search="PAT")) == 2
        assert len(self._names(user_id, search="stamp")) == 2
        assert len(self._names(user_id, search="  ink  ")) == 1

    @pytest.mark.parametrize("term", ["%", "_", "\\", "'; DROP TABLE sales; --"])
    def test_search_treats_special_characters_literally(self, user_id, term):
        SaleFactory(user_id=user_id, product_name="plain")
        db.session.commit()
        assert self._names(user_id, search=term) == []

    def test_search_for_a_literal_percent(self, user_id):
        self._fill(user_id)
        assert [n for n, _ in self._names(user_id, search="0%")] == ["100% Wool"]

    def test_filter_by_dates_inclusive(self, user_id):
        self._fill(user_id)
        names = self._names(user_id, date_from=date(2026, 2, 10), date_to=date(2026, 3, 10))
        assert len(names) == 2

    def test_filter_by_customer(self, user_id):
        self._fill(user_id)
        assert len(self._names(user_id, customer="pat")) == 2

    def test_filter_by_product(self, user_id, product):
        make_sale(user_id, product)
        self._fill(user_id)
        assert self._names(user_id, product_id=product.id) == [("Stamp", None)]

    def test_filter_by_who_received_it(self, user_id, product):
        split.add_person(user_id, name="Alex", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        make_sale(user_id, product)
        sams = make_sale(user_id, product, received_by_partner_id=sam.id)
        result = sales.list_sales(user_id, SaleFilters(received_by_partner_id=sam.id))
        assert [s.id for s in result.items] == [sams.id]

    @pytest.mark.parametrize(
        ("sort", "descending", "first"),
        [
            ("price", True, "30.00"),
            ("price", False, "5.00"),
            ("quantity", True, "5.00"),
            ("profit", False, "5.00"),
            ("profit", True, "30.00"),
        ],
    )
    def test_sorting(self, user_id, sort, descending, first):
        self._fill(user_id)
        result = sales.list_sales(user_id, SaleFilters(sort=sort, descending=descending))
        assert result.items[0].price_paid == Decimal(first)

    def test_customer_sort_puts_blanks_last(self, user_id):
        self._fill(user_id)
        result = sales.list_sales(user_id, SaleFilters(sort="customer", descending=False))
        assert result.items[-1].customer_name is None

    def test_an_unknown_sort_from_a_url_falls_back_to_the_default(self, user_id):
        self._fill(user_id)
        result = sales.list_sales(user_id, SaleFilters(sort="password_hash"))
        assert result.items[0].sale_date == date(2026, 4, 10)

    def test_paging(self, user_id):
        for day in range(1, 8):
            SaleFactory(user_id=user_id, sale_date=date(2026, 1, day))
        db.session.commit()
        first = sales.list_sales(user_id, page=1, per_page=3)
        third = sales.list_sales(user_id, page=3, per_page=3)
        assert [s.sale_date.day for s in first.items] == [7, 6, 5]
        assert [s.sale_date.day for s in third.items] == [1]
        assert first.pages == 3
        assert first.has_next
        assert not first.has_previous
        assert third.has_previous
        assert not third.has_next
        assert first.totals.count == 7, "totals cover every page"

    def test_silly_page_numbers_are_clamped(self, user_id):
        SaleFactory(user_id=user_id)
        db.session.commit()
        assert sales.list_sales(user_id, page=999).page == 1
        assert sales.list_sales(user_id, page=-5).page == 1
        assert sales.list_sales(user_id, per_page=100000).per_page == 100

    def test_paging_ties_are_stable(self, user_id):
        ids = {SaleFactory(user_id=user_id, sale_date=TODAY).id for _ in range(5)}
        db.session.commit()
        seen = [
            sale.id
            for page in (1, 2, 3)
            for sale in sales.list_sales(user_id, page=page, per_page=2).items
        ]
        assert sorted(seen) == sorted(ids)
        assert len(seen) == 5

    def test_iter_sales_ignores_paging(self, user_id):
        for _ in range(30):
            SaleFactory(user_id=user_id)
        db.session.commit()
        assert len(sales.iter_sales(user_id)) == 30


class TestOwnership:
    @pytest.fixture
    def theirs(self, user_id):
        other = UserFactory()
        db.session.commit()
        return SaleFactory(user_id=other.id)

    def test_cannot_see_or_change_another_accounts_sale(self, user_id, theirs):
        calls = [
            lambda: sales.get_sale(user_id, theirs.id),
            lambda: sales.delete_sale(user_id, theirs.id),
            lambda: sales.update_sale(
                user_id, theirs.id, sale_date=TODAY, quantity=1, price_paid="0"
            ),
        ]
        for call in calls:
            with pytest.raises(NotFoundError):
                call()

    def test_bulk_actions_skip_another_accounts_sales(self, user_id, theirs):
        result = sales.delete_sales(user_id, [theirs.id])
        assert result.deleted_ids == ()
        assert db.session.get(Sale, theirs.id).deleted_at is None

    def test_restore_skips_another_accounts_sales(self, user_id, theirs):
        sales.delete_sales(theirs.user_id, [theirs.id])
        assert sales.restore_sales(user_id, [theirs.id]) == 0

    def test_lists_only_your_own(self, user_id, theirs):
        assert sales.list_sales(user_id).totals.count == 0
        assert sales.iter_sales(user_id) == []
