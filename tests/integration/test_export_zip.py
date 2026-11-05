"""The download of everything in an account."""

from __future__ import annotations

import csv
import io
import zipfile

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import AuditLog, User
from app.services import auth, export, notifications, products, sales, split, support
from app.services.notifications import Kind
from tests.conftest import login
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

DAY = "2026-11-05"
URL = "/app/settings/data/export.zip"
EVERY_FILE = {
    "account.csv",
    "people.csv",
    "products.csv",
    "product_cost_changes.csv",
    "sales.csv",
    "upfront_costs.csv",
    "payments.csv",
    "settle_ups.csv",
    "settle_up_people.csv",
    "support_messages.csv",
    "notifications.csv",
    "sign_ins.csv",
}


def read_zip(response):
    """Every file in the zip, as rows. Each one has to open as UTF-8 with a BOM, for Excel."""
    tables = {}
    with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
        assert bundle.testzip() is None
        for name in bundle.namelist():
            raw = bundle.read(name)
            assert raw.startswith(b"\xef\xbb\xbf"), name
            tables[name] = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    return tables


@pytest.fixture
def user_id(app):
    with app.app_context():
        return auth.register("alex", PASSWORD).user.id


@pytest.fixture
def signed_in(client, user_id):
    login(client, "alex", PASSWORD)
    return client


@pytest.fixture
def filled(app, user_id):
    with app.app_context():
        product = products.create_product(user_id, name="Stamp", cost="2.50", sell_price="9")
        products.update_product(user_id, product.id, name="Stamp", cost="3.00", sell_price="9")
        alex = split.add_person(user_id, name="José", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        sale = sales.create_sale(
            user_id,
            product_id=product.id,
            sale_date=DAY,
            quantity=2,
            price_paid="20.00",
            customer_name="=HYPERLINK(1)",
            notes="Line one\nLine two, with a comma",
            received_by_partner_id=sam.id,
        )
        split.add_cost(
            user_id,
            description="Rubber",
            category="stock",
            amount="5.00",
            cost_date=DAY,
            paid_by_partner_id=alex.id,
            product_id=product.id,
        )
        split.record_payment(
            user_id, from_partner_id=sam.id, to_partner_id=alex.id, amount="1.00", paid_on=DAY
        )
        split.settle_up(user_id, actor_id=user_id)
        gone = sales.create_sale(
            user_id,
            product_id=product.id,
            sale_date=DAY,
            quantity=1,
            price_paid="77.77",
            received_by_partner_id=sam.id,
        )
        sales.delete_sale(user_id, gone.id)
        support.send_message(user_id, subject="@subject", body="Body")
        return sale.id


class TestDownload:
    def test_is_a_zip_with_a_dated_name(self, signed_in):
        response = signed_in.get(URL)
        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment; filename=selltrack-export-20")
        assert disposition.endswith(".zip")
        assert int(response.headers["Content-Length"]) == len(response.data)
        assert response.headers["Cache-Control"] == "no-store"

    def test_an_empty_account_still_gets_every_file(self, signed_in):
        tables = read_zip(signed_in.get(URL))
        assert set(tables) == EVERY_FILE
        assert tables["sales.csv"] == [list(export.SALES_HEADER)]
        assert tables["account.csv"][1][0] == "alex"

    def test_holds_everything_in_the_account(self, signed_in, filled):
        tables = read_zip(signed_in.get(URL))
        assert [row[0] for row in tables["people.csv"][1:]] == ["José", "Sam"]
        assert tables["products.csv"][1][:3] == ["Stamp", "3.00", "9.00"]
        changes = tables["product_cost_changes.csv"][1:]
        assert [row[1:3] for row in changes] == [["", "2.50"], ["2.50", "3.00"]]
        assert tables["upfront_costs.csv"][1][1:6] == ["Rubber", "stock", "5.00", "José", "Stamp"]
        assert tables["payments.csv"][1][1:4] == ["Sam", "José", "1.00"]
        assert tables["settle_ups.csv"][1][3:] == ["20.00", "5.00", "15.00"]
        person = tables["settle_up_people.csv"][1]
        assert person[1:4] == ["José", "50.00", "5.00"]
        assert tables["support_messages.csv"][1][3] == "open"
        assert any("Settle-up recorded" in row for row in tables["notifications.csv"][1:])
        assert len(tables["sign_ins.csv"]) == 2

    def test_text_with_commas_and_line_breaks_survives(self, signed_in, filled):
        row = read_zip(signed_in.get(URL))["sales.csv"][1]
        assert row[9] == "Line one\nLine two, with a comma"
        assert row[8] == "Sam"
        assert row[-1] == "Yes"

    def test_deleted_sales_are_left_out(self, signed_in, filled):
        assert "77.77" not in str(read_zip(signed_in.get(URL))["sales.csv"])

    def test_formulas_cant_run_in_any_file(self, app, signed_in, user_id, filled):
        with app.app_context():
            notifications.notify(user_id, Kind.ANNOUNCEMENT, "=TITLE()", "+BODY()")
            split.add_person(user_id, name="@person", share_pct="0")
            products.create_product(user_id, name="-product", cost="1")
            db.session.commit()
        signed_in.get("/app", headers={"User-Agent": "=cmd|' /C calc'!A0"})
        client = app.test_client()
        login(client, "alex", PASSWORD, headers={"User-Agent": "=cmd|' /C calc'!A0"})
        tables = read_zip(signed_in.get(URL))
        for name, rows in tables.items():
            for row in rows[1:]:
                for cell in row:
                    # Plain numbers may be negative. Anything else must not start like a formula.
                    if cell.startswith(("=", "+", "@")) or (
                        cell.startswith("-") and not _is_number(cell)
                    ):
                        pytest.fail(f"{name}: {cell!r}")
        assert any("'=cmd" in row[2] for row in tables["sign_ins.csv"][1:])

    def test_negative_amounts_stay_numbers(self, app, signed_in, filled):
        balances = [row[7] for row in read_zip(signed_in.get(URL))["settle_up_people.csv"][1:]]
        assert any(balance.startswith("-") for balance in balances)
        assert all(_is_number(balance) for balance in balances)

    def test_no_secrets(self, app, signed_in, user_id):
        with app.app_context():
            password_hash = db.session.get(User, user_id).password_hash
        bundle = zipfile.ZipFile(io.BytesIO(signed_in.get(URL).data))
        raw = b"".join(bundle.read(name) for name in EVERY_FILE)
        assert password_hash.encode() not in raw
        assert b"argon2" not in raw

    def test_only_your_own_account(self, app, signed_in, filled):
        with app.app_context():
            other = auth.register("other", PASSWORD).user.id
            product = products.create_product(other, name="THEIR-PRODUCT", cost="1")
            person = split.add_person(other, name="THEIR-PERSON", share_pct="100")
            sales.create_sale(
                other,
                product_id=product.id,
                sale_date=DAY,
                quantity=1,
                price_paid="1",
                received_by_partner_id=person.id,
            )
            support.send_message(other, subject="THEIR-MESSAGE", body="x")
        text = str(read_zip(signed_in.get(URL)))
        assert "THEIR" not in text
        assert "other" not in text

    def test_is_audited(self, app, signed_in, user_id):
        signed_in.get(URL)
        with app.app_context():
            entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "data.exported"))
            assert (entry.actor_id, entry.target_id) == (user_id, user_id)

    def test_is_limited(self, signed_in):
        for _ in range(10):
            assert signed_in.get(URL).status_code == 200
        assert signed_in.get(URL).status_code == 429

    def test_needs_a_login(self, client):
        assert client.get(URL).status_code == 302

    def test_the_page_links_to_it(self, signed_in):
        assert URL in signed_in.get("/app/settings/data").text


class TestSalesCsv:
    def test_opens_properly_in_excel(self, signed_in, filled):
        response = signed_in.get("/app/sales/export.csv")
        assert response.data.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    ("value", "safe"),
    [
        ("=1+1", "'=1+1"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@sum", "'@sum"),
        ("\t=1", "'\t=1"),
        ("\r=1", "'\r=1"),
        ("\n=1", "'\n=1"),
        ("  =1", "'  =1"),
        ("plain", "plain"),
        ("a=b", "a=b"),
        ("", ""),
        (None, ""),
    ],
)
def test_safe_text(value, safe):
    assert export.safe_text(value) == safe


def _is_number(text):
    try:
        float(text)
    except ValueError:
        return False
    return True
