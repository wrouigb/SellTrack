"""Every page, through its real route: what it shows, and what it does when you submit it."""

from __future__ import annotations

import csv
import io
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import Partner, Product, Sale, Settlement, Theme, UpfrontCost, User
from app.services import auth, products, sales, split
from tests.conftest import login, recovery_codes_in, register
from tests.factories import PASSWORD, SaleFactory

pytestmark = pytest.mark.integration

DAY = "2026-11-05"
# The script injection the audit reproduced against the original app's inline confirm().
XSS = "x'); alert(1); ('<script>alert(2)</script>"


@pytest.fixture
def user(app, client):
    register(client, "alex", PASSWORD)
    with app.app_context():
        return db.session.scalar(select(User.id))


@pytest.fixture
def stocked(app, user):
    with app.app_context():
        product = products.create_product(user, name="Stamp", cost="2.00", sell_price="5.00")
        alex = split.add_person(user, name="Alex", share_pct="50")
        sam = split.add_person(user, name="Sam", share_pct="50")
        return {"user": user, "product": product.id, "alex": alex.id, "sam": sam.id}


@pytest.fixture
def other_account(app):
    """Someone else's records, for checking that nobody can reach them."""
    with app.app_context():
        owner = auth.register("stranger", PASSWORD).user.id
        product = products.create_product(owner, name="Their product", cost="1")
        pat = split.add_person(owner, name="Pat", share_pct="50")
        kim = split.add_person(owner, name="Kim", share_pct="50")
        sale = sales.create_sale(
            owner,
            product_id=product.id,
            sale_date=DAY,
            quantity=1,
            price_paid="100",
            received_by_partner_id=pat.id,
        )
        cost = split.add_cost(
            owner,
            description="Theirs",
            category="stock",
            amount="1",
            cost_date=DAY,
            paid_by_partner_id=pat.id,
        )
        payment = split.record_payment(
            owner, from_partner_id=pat.id, to_partner_id=kim.id, amount="1", paid_on=DAY
        )
        settlement = split.settle_up(owner)
        cost2 = split.add_cost(
            owner,
            description="Open",
            category="stock",
            amount="1",
            cost_date=DAY,
            paid_by_partner_id=pat.id,
        )
        return {
            "owner": owner,
            "product": product.id,
            "pat": pat.id,
            "sale": sale.id,
            "cost": cost.id,
            "open_cost": cost2.id,
            "payment": payment.id,
            "settlement": settlement.id,
        }


@contextmanager
def freeze_today(day):
    with patch("app.services.reporting.today_for", return_value=day):
        yield


def add_sale(client, stocked, **values):
    data = {
        "product_id": stocked["product"],
        "sale_date": DAY,
        "quantity": "1",
        "price_paid": "10.00",
        "received_by_partner_id": stocked["sam"],
        **values,
    }
    return client.post("/app/sales/new", data=data)


class TestPublicPages:
    @pytest.mark.parametrize(
        ("path", "text"),
        [
            ("/", "Know what you made"),
            ("/help", "How the split works"),
            ("/privacy", "What's stored"),
            ("/terms", "The figures"),
            ("/login", "Log in"),
            ("/register", "Create an account"),
            ("/recover", "Recover your account"),
        ],
    )
    def test_shows_to_guests(self, client, path, text):
        response = client.get(path)
        assert response.status_code == 200
        assert text in response.text.replace("&#39;", "'")

    def test_landing_sends_logged_in_people_to_the_app(self, client, user):
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"] == "/app"

    def test_every_page_has_the_basics(self, client):
        html = client.get("/").text
        assert '<html lang="en-AU"' in html
        assert 'name="viewport"' in html
        assert 'charset="utf-8"' in html
        assert 'class="skip-link" href="#main"' in html
        assert 'id="main"' in html

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("/sales", "/app/sales"),
            ("/products", "/app/products"),
            ("/split", "/app/split"),
            ("/edit/5", "/app/sales/5/edit"),
            ("/products/5", "/app/products/5"),
        ],
    )
    def test_old_addresses_redirect_to_their_new_homes(self, client, old, new):
        response = client.get(old)
        assert response.status_code == 301
        assert response.headers["Location"] == new


class TestAppRequiresLogin:
    @pytest.mark.parametrize(
        "path",
        [
            "/app",
            "/app/sales",
            "/app/sales/new",
            "/app/sales/1/edit",
            "/app/sales/export.csv",
            "/app/products",
            "/app/products/1",
            "/app/split",
            "/app/split/settle",
            "/app/split/costs",
            "/app/split/costs/1/edit",
            "/app/split/payments",
            "/app/split/payments/1/edit",
            "/app/split/history",
            "/app/split/history/1",
            "/app/split/people",
            "/app/settings",
            "/app/settings/security",
            "/app/settings/preferences",
            "/app/settings/data",
            "/onboarding",
            "/onboarding/people",
            "/onboarding/product",
            "/onboarding/sale",
        ],
    )
    def test_guests_go_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    @pytest.mark.parametrize(
        "path",
        [
            "/app/sales/1/delete",
            "/app/sales/bulk-delete",
            "/app/sales/restore",
            "/app/products/1/archive",
            "/app/products/1/restore",
            "/app/products/1/delete",
            "/app/split/settle",
            "/app/split/costs/1/delete",
            "/app/split/payments/1/delete",
            "/app/split/history/undo",
            "/app/split/people/shares",
            "/app/split/people/even",
            "/app/split/people/1/usual-receiver",
            "/app/split/people/1/archive",
            "/app/split/people/1/restore",
            "/app/split/people/1/delete",
            "/onboarding/finish",
            "/onboarding/people/even",
        ],
    )
    def test_guests_cannot_post(self, client, path):
        response = client.post(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")


class TestDashboard:
    def test_empty_account_gets_directions_not_a_blank_page(self, client, user):
        html = client.get("/app").text
        assert "No sales in this period" in html
        assert "Just you so far" in html
        assert "Add a product so you can record sales." in html
        assert "finished setting up" in html

    def test_shows_figures_chart_and_who_owes_whom(self, client, stocked):
        add_sale(client, stocked, sale_date=date(2026, 1, 15).isoformat(), price_paid="80.00")
        html = client.get("/app?period=all_time").text
        assert "$80.00" in html
        assert "Sam pays Alex" in html
        assert "$40.00" in html
        assert "<svg" in html
        assert "mark-money" in html
        assert "Show this as a table" in html

    def test_chart_values_are_also_in_a_table(self, client, stocked):
        add_sale(client, stocked, sale_date="2026-03-02", price_paid="12.34")
        html = client.get("/app?period=custom&date_from=2026-03-01&date_to=2026-03-03").text
        assert '<td role="cell">2 March 2026</td>' in html
        assert "$12.34" in html

    @pytest.mark.parametrize(
        "period", ["this_month", "last_month", "last_30_days", "this_year", "all_time"]
    )
    def test_each_period_works(self, client, stocked, period):
        add_sale(client, stocked)
        assert client.get(f"/app?period={period}").status_code == 200

    @pytest.mark.parametrize(
        "query",
        [
            "period=nonsense",
            "period=custom&date_from=2026-02-01&date_to=2026-01-01",
            "period=custom&date_from=2026-02-01",
            "period=custom&date_from=not-a-date&date_to=also-not",
            "period=this_month&date_from=%00",
        ],
    )
    def test_bad_periods_do_not_break_the_page(self, client, user, query):
        assert client.get(f"/app?{query}").status_code == 200

    def test_choosing_dates_before_filling_them_in_is_not_an_error(self, client, stocked):
        add_sale(client, stocked, sale_date="2020-01-01", price_paid="999.00")
        html = client.get("/app?period=custom").text
        assert "Choose both" not in html
        assert 'figure-value">$999.00' not in html, "it mustn't quietly show all time"

    def test_one_date_alone_asks_for_the_other(self, client, user):
        assert (
            "Choose both a start and an end date"
            in client.get("/app?period=custom&date_from=2026-01-01").text
        )

    def test_compares_like_with_like(self, client, stocked):
        add_sale(client, stocked)
        with freeze_today(date(2026, 11, 5)):
            html = client.get("/app").text
        assert "vs this time in October" in html

    def test_names_a_finished_month_plainly(self, client, stocked):
        add_sale(client, stocked)
        with freeze_today(date(2026, 11, 5)):
            html = client.get("/app?period=last_month").text
        assert "Same as September" in html, "no sales in either month"
        assert "this time in" not in html

    def test_names_the_year_being_compared(self, client, stocked):
        add_sale(client, stocked)
        with freeze_today(date(2026, 11, 5)):
            assert "vs this time last year" in client.get("/app?period=this_year").text

    def test_says_when_shares_are_off(self, app, client, stocked):
        with app.app_context():
            split.update_person(stocked["user"], stocked["alex"], name="Alex", share_pct="10")
        html = client.get("/app").text
        assert "Shares total 60.00%" in html
        assert "Not worked out yet" in html

    def test_says_when_everyone_is_square(self, client, stocked):
        assert "Everyone is square" in client.get("/app").text

    def test_never_shows_another_accounts_figures(self, client, user, other_account):
        html = client.get("/app?period=all_time").text
        assert "Their product" not in html
        assert "$100.00" not in html


class TestAddingASale:
    def test_needs_a_product_first(self, client, user):
        html = client.get("/app/sales/new").text
        assert "Add a product first" in html

    def test_form_starts_ready_to_use(self, client, stocked):
        html = client.get("/app/sales/new").text
        assert 'value="1"' in html
        assert 'data-cost="2.00"' in html
        assert 'data-price="5.00"' in html
        assert "Who received the money" in html
        assert 'x-data="saleForm"' in html

    def test_saving_goes_back_to_the_list(self, app, client, stocked):
        response = add_sale(client, stocked, customer_name="Pat", notes="Paid cash")
        assert response.status_code == 302
        assert response.headers["Location"] == "/app/sales"
        with app.app_context():
            sale = db.session.scalar(select(Sale))
            assert sale.customer_name == "Pat"
            assert sale.notes == "Paid cash"
            assert sale.cost_each == Decimal("2.00")
            assert sale.received_by_partner_id == stocked["sam"]
        assert "Sale saved." in client.get("/app/sales").text

    def test_save_and_add_another_stays_on_the_form(self, client, stocked):
        response = add_sale(client, stocked, add_another="1")
        assert response.headers["Location"] == "/app/sales/new"

    def test_receiver_is_hidden_with_only_one_person(self, app, client, user):
        with app.app_context():
            products.create_product(user, name="Stamp", cost="1")
            split.add_person(user, name="Solo", share_pct="100")
        assert "Who received the money" not in client.get("/app/sales/new").text

    def test_errors_show_on_their_fields_and_keep_what_was_typed(self, client, stocked):
        response = add_sale(
            client, stocked, price_paid="NaN", customer_name="Keep me", quantity="0"
        )
        assert response.status_code == 422
        html = response.text
        assert "There are 2 problems" in html
        assert 'href="#price_paid"' in html
        assert 'href="#quantity"' in html
        assert 'aria-invalid="true"' in html
        assert 'value="Keep me"' in html
        assert 'value="NaN"' in html

    def test_missing_product_is_explained(self, client, stocked):
        response = add_sale(client, stocked, product_id="")
        assert response.status_code == 422
        assert "Choose one of your products." in response.text

    def test_a_tampered_huge_id_is_just_a_mistake(self, client, stocked):
        response = add_sale(client, stocked, product_id=str(10**30))
        assert response.status_code == 422

    def test_cannot_use_another_accounts_product_or_person(self, client, stocked, other_account):
        assert add_sale(client, stocked, product_id=other_account["product"]).status_code == 422
        response = add_sale(client, stocked, received_by_partner_id=other_account["pat"])
        assert response.status_code == 422


class TestSalesList:
    def test_no_sales_says_what_to_do(self, client, user):
        assert "Start by adding a product" in client.get("/app/sales").text

    def test_lists_with_totals(self, client, stocked):
        add_sale(client, stocked, price_paid="10.00")
        add_sale(client, stocked, price_paid="20.50")
        html = client.get("/app/sales").text
        assert "2 sales" in html
        assert "$30.50 in" in html
        assert "$26.50 profit" in html

    def test_search_filter_and_empty_result(self, client, stocked):
        add_sale(client, stocked, customer_name="Pat")
        add_sale(client, stocked, customer_name="Lee")
        html = client.get("/app/sales?q=pat").text
        assert "1 sale<" in html
        assert ">Pat<" in html
        assert ">Lee<" not in html
        assert "No sales match" in client.get("/app/sales?q=nobody").text

    def test_filters_by_date_product_and_receiver(self, client, stocked):
        add_sale(client, stocked, sale_date="2026-01-15", customer_name="Early")
        add_sale(client, stocked, sale_date="2026-06-15", customer_name="Late")
        html = client.get("/app/sales?date_from=2026-06-01&date_to=2026-06-30").text
        assert ">Late<" in html
        assert ">Early<" not in html
        assert (
            "1 sale<" in client.get(f"/app/sales?received_by={stocked['alex']}").text
            or "0 sales" in client.get(f"/app/sales?received_by={stocked['alex']}").text
        )
        assert "2 sales" in client.get(f"/app/sales?product={stocked['product']}").text

    @pytest.mark.parametrize(
        "query",
        [
            "date_from=nonsense",
            "date_to=2026-13-45",
            "product=abc",
            f"product={10**30}",
            "received_by=abc",
            "sort=password_hash",
            "sort=date&dir=sideways",
            "page=abc",
            "page=-1",
            f"page={10**30}",
            "q=%25%27%3B--",
        ],
    )
    def test_odd_addresses_do_not_break_it(self, client, stocked, query):
        add_sale(client, stocked)
        assert client.get(f"/app/sales?{query}").status_code == 200

    def test_sort_links_keep_the_filters_and_flip_direction(self, client, stocked):
        add_sale(client, stocked)
        html = client.get("/app/sales?q=stamp&sort=price&dir=desc").text
        assert 'aria-sort="descending"' in html
        assert "q=stamp&amp;sort=price&amp;dir=asc" in html

    def test_paging(self, app, client, stocked):
        with app.app_context():
            for _ in range(30):
                SaleFactory(user_id=stocked["user"])
            db.session.commit()
        first = client.get("/app/sales").text
        assert "Page 1 of 2" in first
        assert "page=2" in first
        second = client.get("/app/sales?page=2").text
        assert "Page 2 of 2" in second
        assert "Previous page" in second

    def test_the_page_updates_itself_without_a_full_reload(self, client, stocked):
        add_sale(client, stocked)
        html = client.get("/app/sales").text
        assert 'hx-select="#sales-results"' in html
        assert 'id="sales-results"' in html
        assert 'hx-push-url="true"' in html

    def test_settled_sales_are_marked_and_cannot_be_ticked(self, app, client, stocked):
        add_sale(client, stocked)
        with app.app_context():
            split.settle_up(stocked["user"])
            sale_id = db.session.scalar(select(Sale.id))
        html = client.get("/app/sales").text
        assert "Settled" in html
        assert f'name="ids" value="{sale_id}"' not in html

    def test_only_shows_your_own(self, client, user, other_account):
        html = client.get("/app/sales").text
        assert "Their product" not in html


class TestEditingASale:
    def test_form_shows_current_values_and_locks_the_product(self, client, stocked):
        add_sale(client, stocked, customer_name="Pat", price_paid="10.00")
        html = client.get("/app/sales/1/edit").text
        assert 'value="10.00"' in html
        assert 'value="Pat"' in html
        assert "$2.00 each" in html
        assert 'id="product_id"' not in html

    def test_saves_changes(self, app, client, stocked):
        add_sale(client, stocked)
        response = client.post(
            "/app/sales/1/edit",
            data={
                "sale_date": "2026-01-02",
                "quantity": "3",
                "price_paid": "30.00",
                "customer_name": "New",
                "received_by_partner_id": stocked["alex"],
            },
        )
        assert response.status_code == 302
        with app.app_context():
            sale = db.session.get(Sale, 1)
            assert sale.sale_date == date(2026, 1, 2)
            assert sale.quantity == 3
            assert sale.profit == Decimal("24.00")
            assert sale.received_by_partner_id == stocked["alex"]

    def test_errors_keep_the_form(self, client, stocked):
        add_sale(client, stocked)
        response = client.post(
            "/app/sales/1/edit",
            data={"sale_date": "nope", "quantity": "1", "price_paid": "1"},
        )
        assert response.status_code == 422
        assert "Enter a valid date." in response.text

    def test_settled_sale_locks_its_money_in_the_page_and_on_the_server(self, app, client, stocked):
        add_sale(client, stocked, price_paid="10.00")
        with app.app_context():
            split.settle_up(stocked["user"])
        html = client.get("/app/sales/1/edit").text
        assert "part of a settle-up" in html
        assert html.count("disabled") >= 3

        # Even a tampered form can't change them: locked fields are read from the sale.
        response = client.post(
            "/app/sales/1/edit",
            data={
                "sale_date": DAY,
                "quantity": "99",
                "price_paid": "999.00",
                "customer_name": "Allowed",
                "received_by_partner_id": stocked["alex"],
            },
        )
        assert response.status_code == 302
        with app.app_context():
            sale = db.session.get(Sale, 1)
            assert sale.quantity == 1
            assert sale.price_paid == Decimal("10.00")
            assert sale.received_by_partner_id == stocked["sam"]
            assert sale.customer_name == "Allowed"


class TestDeletingSales:
    def test_delete_then_undo(self, app, client, stocked):
        add_sale(client, stocked)
        response = client.post("/app/sales/1/delete", follow_redirects=True)
        html = response.text
        assert "Sale deleted." in html
        assert "No sales yet" in html
        assert 'action="/app/sales/restore"' in html
        assert 'name="ids" value="1"' in html

        response = client.post("/app/sales/restore", data={"ids": "1"}, follow_redirects=True)
        assert "1 sale restored." in response.text
        with app.app_context():
            assert db.session.get(Sale, 1).deleted_at is None

    def test_the_undo_offer_is_only_shown_once(self, client, stocked):
        add_sale(client, stocked)
        client.post("/app/sales/1/delete", follow_redirects=True)
        assert 'action="/app/sales/restore"' not in client.get("/app/sales").text

    def test_bulk_delete_and_undo(self, client, stocked):
        for _ in range(3):
            add_sale(client, stocked)
        html = client.post(
            "/app/sales/bulk-delete", data={"ids": ["1", "2"]}, follow_redirects=True
        ).text
        assert "2 sales deleted." in html
        assert "1 sale<" in html
        assert (
            "2 sales restored."
            in client.post(
                "/app/sales/restore", data={"ids": ["1", "2"]}, follow_redirects=True
            ).text
        )

    def test_bulk_delete_with_nothing_ticked(self, client, stocked):
        html = client.post("/app/sales/bulk-delete", follow_redirects=True).text
        assert "Tick the sales you want to delete first." in html

    def test_bulk_delete_says_which_were_left_alone(self, app, client, stocked):
        add_sale(client, stocked)
        with app.app_context():
            split.settle_up(stocked["user"])
        add_sale(client, stocked)
        html = client.post(
            "/app/sales/bulk-delete", data={"ids": ["1", "2"]}, follow_redirects=True
        ).text
        assert "1 sale deleted." in html
        assert "1 sale was left alone" in html

    def test_returns_to_the_same_filtered_page(self, client, stocked):
        add_sale(client, stocked)
        response = client.post("/app/sales/1/delete", data={"next": "/app/sales?q=stamp&page=1"})
        assert response.headers["Location"] == "/app/sales?q=stamp&page=1"

    def test_next_cannot_leave_the_site(self, client, stocked):
        add_sale(client, stocked)
        response = client.post("/app/sales/1/delete", data={"next": "https://evil.example/"})
        assert response.headers["Location"] == "/app/sales"

    def test_settled_sale_cannot_be_deleted(self, app, client, stocked):
        add_sale(client, stocked)
        with app.app_context():
            split.settle_up(stocked["user"])
        html = client.post("/app/sales/1/delete", follow_redirects=True).text
        assert "Undo the settle-up to delete it" in html

    def test_get_does_not_delete(self, client, stocked):
        add_sale(client, stocked)
        assert client.get("/app/sales/1/delete").status_code == 405


class TestCsvExport:
    def _rows(self, response):
        return list(csv.reader(io.StringIO(response.text)))

    def test_downloads_every_matching_sale(self, app, client, stocked):
        for number in range(30):
            add_sale(client, stocked, customer_name=f"Customer {number}", price_paid="10.00")
        response = client.get("/app/sales/export.csv")
        assert response.mimetype == "text/csv"
        assert "attachment" in response.headers["Content-Disposition"]
        assert ".csv" in response.headers["Content-Disposition"]
        rows = self._rows(response)
        assert len(rows) == 31, "all of them, not just one page"
        assert rows[0][:4] == ["Date", "Product", "Customer", "Quantity"]

    def test_uses_the_current_filters(self, client, stocked):
        add_sale(client, stocked, customer_name="Pat")
        add_sale(client, stocked, customer_name="Lee")
        rows = self._rows(client.get("/app/sales/export.csv?q=pat"))
        assert len(rows) == 2
        assert rows[1][2] == "Pat"

    def test_values(self, client, stocked):
        add_sale(
            client, stocked, sale_date="2026-03-04", quantity="2", price_paid="3.00", notes="Hi"
        )
        row = self._rows(client.get("/app/sales/export.csv"))[1]
        assert row == [
            "2026-03-04",
            "Stamp",
            "",
            "2",
            "2.00",
            "4.00",
            "3.00",
            "-1.00",
            "Sam",
            "Hi",
            "No",
        ]

    def test_says_when_a_sale_is_settled(self, app, client, stocked):
        add_sale(client, stocked)
        with app.app_context():
            split.settle_up(stocked["user"])
        assert self._rows(client.get("/app/sales/export.csv"))[1][-1] == "Yes"

    @pytest.mark.parametrize(
        "evil", ['=HYPERLINK("http://evil")', "+1+1", "-2+3", "@SUM(1)", "\tx", "\rx"]
    )
    def test_spreadsheet_formulas_are_defused(self, client, stocked, evil):
        add_sale(client, stocked, customer_name=f"{evil}", notes=f"{evil}")
        row = self._rows(client.get("/app/sales/export.csv"))[1]
        assert not row[2].startswith(("=", "+", "-", "@"))
        assert not row[9].startswith(("=", "+", "-", "@"))

    def test_only_exports_your_own(self, client, user, other_account):
        assert "Their product" not in client.get("/app/sales/export.csv").text

    def test_nothing_to_export_is_still_a_valid_file(self, client, user):
        assert len(self._rows(client.get("/app/sales/export.csv"))) == 1


class TestProducts:
    def test_add_form_is_open_when_there_are_none(self, client, user):
        html = client.get("/app/products").text
        assert '<details class="panel add-panel mb-6" open>' in html
        assert "No products yet" in html

    def test_add(self, app, client, user):
        response = client.post(
            "/app/products", data={"name": "Stamp", "cost": "2.50", "sell_price": "10"}
        )
        assert response.status_code == 302
        html = client.get("/app/products").text
        assert "Stamp added." in html
        assert "$2.50" in html
        assert "75.0%" in html

    def test_errors_are_shown_and_keep_what_was_typed(self, client, user):
        response = client.post("/app/products", data={"name": "Stamp", "cost": "NaN"})
        assert response.status_code == 422
        assert 'value="Stamp"' in response.text
        assert '<details class="panel add-panel mb-6" open>' in response.text

    def test_duplicate_name(self, client, user):
        client.post("/app/products", data={"name": "Stamp", "cost": "1"})
        response = client.post("/app/products", data={"name": "STAMP", "cost": "1"})
        assert response.status_code == 422
        assert "already have a product" in response.text

    def test_search_sort_and_odd_addresses(self, client, stocked):
        client.post("/app/products", data={"name": "Ink", "cost": "1"})
        assert "Ink" in client.get("/app/products?q=ink").text
        assert ">Stamp" not in client.get("/app/products?q=ink").text
        assert "No products match" in client.get("/app/products?q=zzz").text
        assert client.get("/app/products?sort=cost&dir=desc").status_code == 200
        assert client.get("/app/products?sort=password_hash").status_code == 200

    def test_edit_logs_cost_changes(self, client, stocked):
        url = f"/app/products/{stocked['product']}"
        html = client.get(url).text
        assert "First cost" in html
        assert 'value="2.00"' in html
        response = client.post(url, data={"name": "Stamp", "cost": "3.00", "sell_price": "5.00"})
        assert response.status_code == 302
        history = client.get(url).text
        assert "$2.00" in history
        assert "$3.00" in history

    def test_edit_errors(self, client, stocked):
        response = client.post(
            f"/app/products/{stocked['product']}", data={"name": "", "cost": "1"}
        )
        assert response.status_code == 422
        assert "The name can&#39;t be empty." in response.text

    def test_archive_then_restore(self, client, stocked):
        url = f"/app/products/{stocked['product']}"
        assert "Stamp archived." in client.post(f"{url}/archive", follow_redirects=True).text
        assert "Stamp" not in client.get("/app/products").text
        assert "Archived" in client.get("/app/products?archived=y").text
        assert "Restore product" in client.get(url).text
        client.post(f"{url}/restore")
        assert "Stamp" in client.get("/app/products").text

    def test_restore_blocked_by_a_taken_name(self, client, stocked):
        url = f"/app/products/{stocked['product']}"
        client.post(f"{url}/archive")
        client.post("/app/products", data={"name": "Stamp", "cost": "1"})
        html = client.post(f"{url}/restore", follow_redirects=True).text
        assert "already have a product" in html

    def test_delete_keeps_past_sales(self, app, client, stocked):
        add_sale(client, stocked)
        client.post(f"/app/products/{stocked['product']}/delete")
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(Product)) == 0
            assert db.session.scalar(select(Sale.product_name)) == "Stamp"


class TestSplitOverview:
    def _worked_example(self, client, stocked):
        """Plan section 3.3, through the pages."""
        for description, amount, payer in (
            ("Stock", "35.00", stocked["alex"]),
            ("Stall fee", "10.00", stocked["sam"]),
        ):
            assert (
                client.post(
                    "/app/split/costs",
                    data={
                        "description": description,
                        "category": "stock",
                        "amount": amount,
                        "cost_date": DAY,
                        "paid_by_partner_id": payer,
                    },
                ).status_code
                == 302
            )
        add_sale(client, stocked, price_paid="80.00", received_by_partner_id=stocked["sam"])
        add_sale(client, stocked, price_paid="40.00", received_by_partner_id=stocked["alex"])

    def test_nobody_yet(self, client, user):
        assert "Nobody in the split yet" in client.get("/app/split").text

    def test_worked_example(self, client, stocked):
        self._worked_example(client, stocked)
        html = client.get("/app/split").text
        assert "Sam pays Alex" in html
        assert "$32.50" in html
        assert "$75.00" in html
        assert "Owed $32.50" in html
        assert "Owes $32.50" in html
        assert "$72.50" in html
        assert "Settle up" in html
        assert "Why doesn't this match" in html

    def test_warns_about_shares(self, app, client, stocked):
        add_sale(client, stocked)
        with app.app_context():
            split.update_person(stocked["user"], stocked["alex"], name="Alex", share_pct="10")
        html = client.get("/app/split").text
        assert "Shares total 60.00%" in html
        assert 'href="/app/split/settle"' not in html

    def test_settle_then_undo(self, app, client, stocked):
        self._worked_example(client, stocked)

        page = client.get("/app/split/settle").text
        assert "The payments to make" in page
        assert "Sam pays Alex" in page

        response = client.post("/app/split/settle", follow_redirects=True)
        html = response.text
        assert "Settled up. The tally is back to $0.00." in html
        assert "Sam paid Alex" in html
        assert "Undo this settle-up" in html
        assert "Owed $32.50" in html, "balances are in words, not just a sign or a colour"
        assert "Owes $32.50" in html
        assert "Everyone is square" in client.get("/app/split").text

        with app.app_context():
            settlement_id = db.session.scalar(select(Settlement.id))
        history = client.get("/app/split/history").text
        assert f"/app/split/history/{settlement_id}" in history
        assert "Undo last settle-up" in history

        response = client.post("/app/split/history/undo", follow_redirects=True)
        assert "Settle-up undone." in response.text
        assert "Sam pays Alex" in response.text
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(Settlement)) == 0

    def test_undo_tells_you_shares_came_back(self, app, client, stocked):
        add_sale(client, stocked)
        client.post("/app/split/settle")
        with app.app_context():
            split.update_person(stocked["user"], stocked["alex"], name="Alex", share_pct="70")
            split.update_person(stocked["user"], stocked["sam"], name="Sam", share_pct="30")
        html = client.post("/app/split/history/undo", follow_redirects=True).text
        assert "Shares are back to what they were" in html

    def test_undo_name_clash_is_explained(self, app, client, stocked):
        add_sale(client, stocked)
        client.post("/app/split/settle")
        with app.app_context():
            split.archive_person(stocked["user"], stocked["sam"])
            split.add_person(stocked["user"], name="sam", share_pct="50")
        html = client.post("/app/split/history/undo", follow_redirects=True).text
        assert "someone else has that name now" in html

    def test_undo_with_nothing_to_undo(self, client, stocked):
        html = client.post("/app/split/history/undo", follow_redirects=True).text
        assert "no settle-up to undo" in html

    def test_settling_when_you_cannot(self, client, stocked):
        html = client.post("/app/split/settle", follow_redirects=True).text
        assert "nothing to settle" in html
        assert "nothing you can settle yet" in client.get("/app/split/settle").text

    def test_older_settlements_cannot_be_undone_from_their_page(self, app, client, stocked):
        add_sale(client, stocked)
        client.post("/app/split/settle")
        add_sale(client, stocked)
        client.post("/app/split/settle")
        with app.app_context():
            first, second = db.session.scalars(select(Settlement.id).order_by(Settlement.id)).all()
        assert "Undo this settle-up" not in client.get(f"/app/split/history/{first}").text
        assert "Undo this settle-up" in client.get(f"/app/split/history/{second}").text


class TestCostsAndPayments:
    def test_costs_need_people_first(self, client, user):
        assert "Add people first" in client.get("/app/split/costs").text

    def test_add_edit_delete_cost(self, app, client, stocked):
        form = {
            "description": "Stall fee",
            "category": "fees",
            "amount": "10.00",
            "cost_date": DAY,
            "paid_by_partner_id": stocked["alex"],
            "product_id": stocked["product"],
            "notes": "Saturday",
        }
        assert client.post("/app/split/costs", data=form).status_code == 302
        html = client.get("/app/split/costs").text
        assert "Stall fee" in html
        assert "Fees" in html
        assert "$10.00" in html

        assert 'value="Stall fee"' in client.get("/app/split/costs/1/edit").text
        changed = {**form, "amount": "12.00", "paid_by_partner_id": stocked["sam"]}
        assert client.post("/app/split/costs/1/edit", data=changed).status_code == 302
        with app.app_context():
            cost = db.session.get(UpfrontCost, 1)
            assert cost.amount == Decimal("12.00")
            assert cost.paid_by_partner_id == stocked["sam"]

        client.post("/app/split/costs/1/delete")
        assert "No upfront costs yet" in client.get("/app/split/costs").text

    def test_cost_errors(self, client, stocked):
        response = client.post(
            "/app/split/costs",
            data={"description": "", "category": "yachts", "amount": "0", "cost_date": "x"},
        )
        assert response.status_code == 422
        html = response.text
        assert "There are " in html
        assert '<details class="panel add-panel" open>' in html

    def test_settled_cost_locks_money(self, app, client, stocked):
        client.post(
            "/app/split/costs",
            data={
                "description": "Stock",
                "category": "stock",
                "amount": "10.00",
                "cost_date": DAY,
                "paid_by_partner_id": stocked["alex"],
            },
        )
        client.post("/app/split/settle")
        assert "part of a settle-up" in client.get("/app/split/costs/1/edit").text
        client.post(
            "/app/split/costs/1/edit",
            data={
                "description": "Renamed",
                "category": "stock",
                "amount": "99.00",
                "cost_date": DAY,
                "paid_by_partner_id": stocked["sam"],
            },
        )
        with app.app_context():
            cost = db.session.get(UpfrontCost, 1)
            assert cost.description == "Renamed"
            assert cost.amount == Decimal("10.00")
            assert cost.paid_by_partner_id == stocked["alex"]
        html = client.post("/app/split/costs/1/delete", follow_redirects=True).text
        assert "part of a settle-up" in html

    def test_payments_need_two_people(self, app, client, user):
        with app.app_context():
            split.add_person(user, name="Solo", share_pct="100")
        assert "Add a second person first" in client.get("/app/split/payments").text

    def test_record_edit_delete_payment(self, client, stocked):
        form = {
            "from_partner_id": stocked["sam"],
            "to_partner_id": stocked["alex"],
            "amount": "20.00",
            "paid_on": DAY,
            "note": "Part payment",
        }
        assert client.post("/app/split/payments", data=form).status_code == 302
        assert "Sam paid Alex" in client.get("/app/split/payments").text
        assert 'value="Part payment"' in client.get("/app/split/payments/1/edit").text
        changed = {**form, "amount": "25.00"}
        assert client.post("/app/split/payments/1/edit", data=changed).status_code == 302
        assert "$25.00" in client.get("/app/split/payments").text
        client.post("/app/split/payments/1/delete")
        assert "No payments yet" in client.get("/app/split/payments").text

    def test_suggested_transfer_fills_in_the_form(self, client, stocked):
        html = client.get(
            f"/app/split/payments?from={stocked['sam']}&to={stocked['alex']}&amount=32.50"
        ).text
        assert 'value="32.50"' in html
        assert '<details class="panel add-panel" open>' in html

    def test_payment_errors(self, client, stocked):
        response = client.post(
            "/app/split/payments",
            data={
                "from_partner_id": stocked["alex"],
                "to_partner_id": stocked["alex"],
                "amount": "1",
                "paid_on": DAY,
            },
        )
        assert response.status_code == 422
        assert "two different people" in response.text

    def test_settled_payment_locks_money(self, app, client, stocked):
        client.post(
            "/app/split/payments",
            data={
                "from_partner_id": stocked["sam"],
                "to_partner_id": stocked["alex"],
                "amount": "5.00",
                "paid_on": DAY,
            },
        )
        client.post("/app/split/settle")
        assert "part of a settle-up" in client.get("/app/split/payments/1/edit").text
        response = client.post(
            "/app/split/payments/1/edit",
            data={
                "from_partner_id": stocked["alex"],
                "to_partner_id": stocked["sam"],
                "amount": "50.00",
                "paid_on": DAY,
                "note": "Noted",
            },
        )
        assert response.status_code == 302
        html = client.get("/app/split/payments").text
        assert "Sam paid Alex" in html
        assert "$5.00" in html
        assert "Noted" in html
        deleted = client.post("/app/split/payments/1/delete", follow_redirects=True).text
        assert "part of a settle-up" in deleted


class TestPeople:
    def test_add(self, client, user):
        html = client.get("/app/split/people").text
        assert '<details class="panel add-panel" open>' in html
        response = client.post(
            "/app/split/people", data={"name": "Alex", "share_pct": "100"}, follow_redirects=True
        )
        assert "Alex added." in response.text
        assert "Usually receives the money" in response.text

    def test_add_errors(self, client, user):
        response = client.post("/app/split/people", data={"name": "", "share_pct": "101"})
        assert response.status_code == 422
        assert "There are 2 problems" in response.text

    def test_add_someone_as_the_usual_receiver(self, app, client, stocked):
        client.post(
            "/app/split/people",
            data={"name": "Third", "share_pct": "0", "usually_receives_money": "y"},
        )
        with app.app_context():
            usual = db.session.scalar(select(Partner).where(Partner.receives_money_default))
            assert usual.name == "Third"

    def test_save_names_and_shares_together(self, app, client, stocked):
        alex, sam = stocked["alex"], stocked["sam"]
        response = client.post(
            "/app/split/people/shares",
            data={
                f"name_{alex}": "Alexandra",
                f"share_{alex}": "60",
                f"name_{sam}": "Sam",
                f"share_{sam}": "40",
            },
        )
        assert response.status_code == 302
        with app.app_context():
            person = db.session.get(Partner, alex)
            assert person.name == "Alexandra"
            assert person.share_pct == Decimal("60.00")

    def test_a_bad_share_saves_nothing_and_keeps_what_was_typed(self, app, client, stocked):
        alex, sam = stocked["alex"], stocked["sam"]
        response = client.post(
            "/app/split/people/shares",
            data={
                f"name_{alex}": "Changed",
                f"share_{alex}": "60",
                f"name_{sam}": "Sam",
                f"share_{sam}": "abc",
            },
        )
        assert response.status_code == 422
        assert 'value="Changed"' in response.text
        assert 'value="abc"' in response.text
        assert f'href="#share_{sam}"' in response.text
        with app.app_context():
            assert db.session.get(Partner, alex).name == "Alex"

    def test_a_name_clash_saves_nothing(self, app, client, stocked):
        alex, sam = stocked["alex"], stocked["sam"]
        response = client.post(
            "/app/split/people/shares",
            data={
                f"name_{alex}": "Same",
                f"share_{alex}": "50",
                f"name_{sam}": "same",
                f"share_{sam}": "50",
            },
        )
        assert response.status_code == 422
        assert "can&#39;t have the same name" in response.text

    def test_even_split(self, app, client, stocked):
        client.post("/app/split/people", data={"name": "Third", "share_pct": "0"})
        client.post("/app/split/people/even")
        with app.app_context():
            shares = db.session.scalars(select(Partner.share_pct).order_by(Partner.id)).all()
        assert sum(shares) == Decimal("100.00")

    def test_usual_receiver(self, app, client, stocked):
        client.post(f"/app/split/people/{stocked['sam']}/usual-receiver")
        with app.app_context():
            assert db.session.get(Partner, stocked["sam"]).receives_money_default

    def test_archive_blocked_until_settled_then_restore(self, client, stocked):
        add_sale(client, stocked)
        url = f"/app/split/people/{stocked['sam']}"
        html = client.post(f"{url}/archive", follow_redirects=True).text
        assert "Settle up first." in html
        client.post("/app/split/settle")
        html = client.post(f"{url}/archive", follow_redirects=True).text
        assert "Sam archived." in html
        assert "Archived" in html
        html = client.post(f"{url}/restore", follow_redirects=True).text
        assert "Sam restored" in html

    def test_remove(self, client, stocked):
        html = client.post(f"/app/split/people/{stocked['sam']}/delete", follow_redirects=True).text
        assert "Person removed." in html

    def test_remove_blocked_by_history(self, client, stocked):
        add_sale(client, stocked)
        html = client.post(f"/app/split/people/{stocked['sam']}/delete", follow_redirects=True).text
        assert "only be archived" in html


class TestOwnership:
    """Nobody can reach another account's records through any page."""

    @pytest.mark.parametrize(
        ("method", "template"),
        [
            ("get", "/app/sales/{sale}/edit"),
            ("post", "/app/sales/{sale}/edit"),
            ("post", "/app/sales/{sale}/delete"),
            ("get", "/app/products/{product}"),
            ("post", "/app/products/{product}"),
            ("post", "/app/products/{product}/archive"),
            ("post", "/app/products/{product}/restore"),
            ("post", "/app/products/{product}/delete"),
            ("get", "/app/split/costs/{cost}/edit"),
            ("post", "/app/split/costs/{cost}/edit"),
            ("post", "/app/split/costs/{open_cost}/delete"),
            ("get", "/app/split/payments/{payment}/edit"),
            ("post", "/app/split/payments/{payment}/edit"),
            ("post", "/app/split/payments/{payment}/delete"),
            ("get", "/app/split/history/{settlement}"),
            ("post", "/app/split/people/{pat}/usual-receiver"),
            ("post", "/app/split/people/{pat}/archive"),
            ("post", "/app/split/people/{pat}/restore"),
            ("post", "/app/split/people/{pat}/delete"),
        ],
    )
    def test_is_a_plain_404(self, client, user, other_account, method, template):
        path = template.format(**other_account)
        response = getattr(client, method)(path)
        assert response.status_code == 404
        assert "Page not found" in response.text

    def test_bulk_actions_skip_them(self, app, client, user, other_account):
        client.post("/app/sales/bulk-delete", data={"ids": other_account["sale"]})
        with app.app_context():
            assert db.session.get(Sale, other_account["sale"]).deleted_at is None

    def test_undo_only_affects_your_own(self, app, client, user, other_account):
        client.post("/app/split/history/undo")
        with app.app_context():
            assert db.session.get(Settlement, other_account["settlement"]) is not None

    def test_their_people_are_not_offered_in_any_form(self, client, stocked, other_account):
        for path in ("/app/sales/new", "/app/split/costs", "/app/split/payments", "/app/sales"):
            assert ">Pat<" not in client.get(path).text


class TestEscaping:
    """Audit issue 6: a product name could run script through the original's inline confirm()."""

    def test_names_are_never_treated_as_html_or_script(self, app, client, user):
        with app.app_context():
            product = products.create_product(user, name=XSS[:100], cost="1")
            alex = split.add_person(user, name=XSS[:50], share_pct="50")
            sam = split.add_person(user, name="Sam", share_pct="50")
            sale = sales.create_sale(
                user,
                product_id=product.id,
                sale_date=DAY,
                quantity=1,
                price_paid="10",
                customer_name=XSS[:100],
                notes=XSS,
                received_by_partner_id=alex.id,
            )
            cost = split.add_cost(
                user,
                description=XSS,
                category="stock",
                amount="1",
                cost_date=DAY,
                paid_by_partner_id=alex.id,
                notes=XSS,
            )
            split.record_payment(
                user,
                from_partner_id=alex.id,
                to_partner_id=sam.id,
                amount="1",
                paid_on=DAY,
                note=XSS,
            )
            sale_id, cost_id, product_id = sale.id, cost.id, product.id

        pages = [
            "/app",
            "/app/sales",
            f"/app/sales/{sale_id}/edit",
            "/app/sales/new",
            "/app/products",
            f"/app/products/{product_id}",
            "/app/split",
            "/app/split/settle",
            "/app/split/costs",
            f"/app/split/costs/{cost_id}/edit",
            "/app/split/payments",
            "/app/split/people",
        ]
        for path in pages:
            response = client.get(path)
            assert response.status_code == 200, path
            assert "<script>alert(2)" not in response.text, path

        client.post("/app/split/settle")
        for path in ("/app/split/history", "/app/split/history/1"):
            assert "<script>alert(2)" not in client.get(path).text, path

    def test_confirmation_text_cannot_break_out_of_its_attribute(self, app, client, user):
        with app.app_context():
            products.create_product(user, name='x" onmouseover="alert(1)', cost="1")
            product_id = db.session.scalar(select(Product.id))
        html = client.get(f"/app/products/{product_id}").text
        assert 'onmouseover="alert(1)"' not in html
        assert "&#34;" in html or "&quot;" in html


class TestSettings:
    def test_account(self, client, user):
        html = client.get("/app/settings").text
        assert "alex" in html
        assert "Member since" in html

    def test_preferences_change_the_theme_and_time_zone(self, app, client, user):
        html = client.get("/app/settings/preferences").text
        assert "Australia: Melbourne" in html
        assert 'data-theme="system"' in html

        response = client.post(
            "/app/settings/preferences", data={"theme": "dark", "timezone": "Asia/Tokyo"}
        )
        assert response.status_code == 302
        with app.app_context():
            person = db.session.scalar(select(User))
            assert person.theme is Theme.DARK
            assert person.timezone == "Asia/Tokyo"
        assert 'data-theme="dark"' in client.get("/app/settings/preferences").text

    @pytest.mark.parametrize(
        "data",
        [
            {"theme": "neon", "timezone": "Asia/Tokyo"},
            {"theme": "dark", "timezone": "Mars/Olympus"},
            {"theme": "dark", "timezone": "../../etc/passwd"},
        ],
    )
    def test_preferences_reject_nonsense(self, app, client, user, data):
        assert client.post("/app/settings/preferences", data=data).status_code == 422
        with app.app_context():
            assert db.session.scalar(select(User)).theme is Theme.SYSTEM

    def test_sign_in_times_use_your_time_zone(self, app, client, user):
        client.post("/logout")
        login(client, "alex", PASSWORD)
        client.post("/app/settings/preferences", data={"theme": "system", "timezone": "Asia/Tokyo"})
        assert "/" in client.get("/app/settings/security").text

    def test_security_has_everything(self, client, user):
        html = client.get("/app/settings/security").text
        assert "Change password" in html
        assert "Log out everywhere" in html
        assert "Create new codes" in html
        assert "Recent sign-ins" in html

    def test_delete_account_needs_the_password(self, app, client, user):
        response = client.post("/app/settings/data", data={"password": "not-my-password"})
        assert response.status_code == 422
        assert "isn&#39;t your current password" in response.text
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(User)) == 1

    def test_delete_account_removes_everything_and_logs_out(self, app, client, stocked):
        add_sale(client, stocked)
        response = client.post(
            "/app/settings/data", data={"password": PASSWORD}, follow_redirects=True
        )
        assert "have been deleted" in response.text
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(User)) == 0
            assert db.session.scalar(select(func.count()).select_from(Sale)) == 0
        assert client.get("/app").status_code == 302


class TestOnboarding:
    def test_recovery_codes_lead_to_it(self, client):
        html = register(client, "alex", PASSWORD).text
        assert len(recovery_codes_in(html)) == 10
        assert 'href="/onboarding"' in html

    def test_full_walkthrough(self, app, client, user):
        assert "Where are you?" in client.get("/onboarding").text
        response = client.post("/onboarding", data={"timezone": "Australia/Perth"})
        assert response.headers["Location"] == "/onboarding/people"

        assert "Who shares the profit?" in client.get("/onboarding/people").text
        client.post("/onboarding/people", data={"name": "Alex", "share_pct": "50"})
        html = client.post(
            "/onboarding/people", data={"name": "Sam", "share_pct": "40"}, follow_redirects=True
        ).text
        assert "Needs to total 100%" in html
        html = client.post("/onboarding/people/even", follow_redirects=True).text
        assert "Needs to total 100%" not in html

        assert "What do you sell?" in client.get("/onboarding/product").text
        response = client.post(
            "/onboarding/product", data={"name": "Stamp", "cost": "2", "sell_price": "5"}
        )
        assert response.headers["Location"] == "/onboarding/sale"

        html = client.get("/onboarding/sale").text
        assert "Record your first sale" in html
        assert 'data-price="5.00"' in html
        with app.app_context():
            product_id = db.session.scalar(select(Product.id))
            first_person = db.session.scalar(select(Partner.id).order_by(Partner.id))
        response = client.post(
            "/onboarding/sale",
            data={
                "product_id": product_id,
                "sale_date": DAY,
                "quantity": "1",
                "price_paid": "5.00",
                "received_by_partner_id": first_person,
            },
            follow_redirects=True,
        )
        assert "You&#39;re all set." in response.text
        with app.app_context():
            person = db.session.scalar(select(User))
            assert person.onboarded_at is not None
            assert person.timezone == "Australia/Perth"
        assert "finished setting up" not in client.get("/app").text

    def test_can_skip_at_any_point(self, app, client, user):
        response = client.post("/onboarding/finish")
        assert response.headers["Location"] == "/app"
        with app.app_context():
            assert db.session.scalar(select(User)).onboarded_at is not None

    def test_errors_at_each_step(self, client, user):
        assert client.post("/onboarding", data={"timezone": "Mars/Base"}).status_code == 422
        assert (
            client.post("/onboarding/people", data={"name": "", "share_pct": "x"}).status_code
            == 422
        )
        assert client.post("/onboarding/product", data={"name": "", "cost": "x"}).status_code == 422
        assert client.post("/onboarding/sale", data={"product_id": ""}).status_code == 422

    def test_sale_step_without_a_product(self, client, user):
        assert "You'll need a product" in client.get("/onboarding/sale").text


class TestErrorPagesLookRight:
    def test_a_logged_in_persons_404_still_works_and_uses_no_database(self, client, user):
        response = client.get("/no-such-page")
        assert response.status_code == 404
        assert "Back to SellTrack" in response.text
        assert 'data-theme="system"' in response.text
