"""The journeys that matter most, from start to finish, in a real browser with JavaScript on."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from app.extensions import db
from app.models import FeatureFlag
from app.services import auth, flags
from tests.e2e.conftest import ACCOUNT
from tests.factories import PASSWORD

pytestmark = pytest.mark.e2e

NEW_PASSWORD = "a-different-passphrase"


def sign_up(page, username):
    page.goto("/register")
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.fill("#confirm", PASSWORD)
    page.click("main button[type=submit]")
    page.wait_for_selector(".codes code")
    return page.locator(".codes code").all_inner_texts()


def log_in(page, username, password=PASSWORD):
    page.goto("/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("main button[type=submit]")
    page.wait_for_url("**/app")


def test_sign_up_to_first_sale_on_the_dashboard(new_page):
    page = new_page()
    codes = sign_up(page, "journey-one")
    assert len(codes) == 10
    page.click("text=I've saved them, continue")

    page.select_option("#timezone", "Australia/Sydney")
    page.click("text=Continue")
    page.fill("#name", "Ana")
    page.fill("#share_pct", "100")
    page.click("text=Add person")
    page.click("text=Continue")
    page.fill("#name", "Mug")
    page.fill("#cost", "4.00")
    page.fill("#sell_price", "15.00")
    page.click("text=Add product")
    # The price fills itself in from the product.
    expect(page.locator("#price_paid")).to_have_value("15.00")
    page.click("main form button[type=submit] >> nth=0")
    page.wait_for_selector("text=You're all set.")

    page.goto("/app?period=all_time")
    figures = page.locator(".figures")
    expect(figures).to_contain_text("$15.00")
    expect(figures).to_contain_text("$11.00")
    expect(page.locator("main")).to_contain_text("Mug")


def test_cost_payment_settle_up_and_undo(new_page):
    page = new_page()
    sign_up(page, "journey-two")
    page.click("text=I've saved them, continue")

    def toast(text):
        expect(page.locator(".toast").first).to_contain_text(text)

    def open_the_form():
        if not page.locator("details.add-panel[open]").count():
            page.click(".add-summary")

    page.goto("/app/split/people")
    for name, share in (("Alex", "50"), ("Sam", "50")):
        open_the_form()
        page.fill("details.add-panel #name", name)
        page.fill("details.add-panel #share_pct", share)
        page.click("details.add-panel button[type=submit]")
        toast(f"{name} added.")

    page.goto("/app/products")
    open_the_form()
    page.fill("#name", "Stamp")
    page.fill("#cost", "2.00")
    page.fill("#sell_price", "120.00")
    page.click("details.add-panel button[type=submit]")
    expect(page.locator("main")).to_contain_text("Stamp")

    page.goto("/app/sales/new")
    page.select_option("#product_id", label="Stamp")
    page.select_option("#received_by_partner_id", label="Sam")
    page.click("main form button[type=submit] >> nth=0")
    toast("Sale saved.")

    page.goto("/app/split/costs")
    open_the_form()
    page.fill("#description", "Stock")
    page.fill("#amount", "20.00")
    page.select_option("#paid_by_partner_id", label="Alex")
    page.click("details.add-panel button[type=submit]")
    toast("Upfront cost added.")

    page.goto("/app/split/payments")
    open_the_form()
    page.select_option("#from_partner_id", label="Sam")
    page.select_option("#to_partner_id", label="Alex")
    page.fill("#amount", "10.00")
    page.click("details.add-panel button[type=submit]")
    toast("Payment recorded.")

    # $120 came in, Alex gets $20 back, and the $100 profit is shared evenly. So Alex should end
    # with $70 and is holding $10, and Sam should end with $50 and is holding $110.
    page.goto("/app/split")
    tag = page.locator(".tag").first
    expect(tag).to_contain_text("Sam pays Alex")
    expect(tag).to_contain_text("$60.00")

    page.goto("/app/split/settle")
    page.click("main button:has-text('Settle up')")
    toast("Settled up.")
    page.goto("/app/split/history")
    expect(page.locator("main tbody tr")).to_have_count(1)
    expect(page.locator("main tbody")).to_contain_text("$120.00")

    page.click("main button:has-text('Undo last settle-up')")
    page.click("#confirm-dialog [data-accept]")
    toast("Settle-up undone.")
    page.goto("/app/split")
    expect(page.locator(".tag").first).to_contain_text("$60.00")
    page.goto("/app/split/history")
    expect(page.locator("main tbody tr")).to_have_count(0)


def test_recovering_an_account_with_a_code(new_page):
    page = new_page()
    codes = sign_up(page, "journey-three")
    page.click("text=I've saved them, continue")

    stranger = new_page()
    stranger.goto("/recover")
    stranger.fill("#username", "journey-three")
    stranger.fill("#code", codes[0])
    stranger.fill("#new_password", NEW_PASSWORD)
    stranger.fill("#confirm", NEW_PASSWORD)
    stranger.click("main button[type=submit]")
    stranger.wait_for_url("**/login")
    expect(stranger.locator(".toast").first).to_contain_text("password has been changed")
    log_in(stranger, "journey-three", NEW_PASSWORD)

    # The old session ended, and that code can't be used twice.
    page.goto("/app")
    assert "/login" in page.url
    again = new_page()
    again.goto("/recover")
    again.fill("#username", "journey-three")
    again.fill("#code", codes[0])
    again.fill("#new_password", NEW_PASSWORD + "2")
    again.fill("#confirm", NEW_PASSWORD + "2")
    again.click("main button[type=submit]")
    expect(again.locator(".toast").first).to_contain_text("Wrong username or recovery code")


def test_admin_suspends_rolls_out_a_flag_and_answers_support(
    new_page, admin_state, logged_in_state, e2e_app
):
    with e2e_app.app_context():
        target = auth.register("journey-four", PASSWORD).user.id
        db.session.remove()
    person = new_page()
    log_in(person, "journey-four")

    admin = new_page(state=admin_state)
    admin.goto(f"/admin/users/{target}")
    admin.click("main button:has-text('Suspend account')")
    admin.click("#confirm-dialog [data-accept]")
    expect(admin.locator(".toast").first).to_contain_text("Account suspended")

    person.goto("/app")
    assert "/login" in person.url
    person.fill("#username", "journey-four")
    person.fill("#password", PASSWORD)
    person.click("main button[type=submit]")
    expect(person.locator(".toast").first).to_contain_text("suspended")

    admin.goto("/admin/flags")
    if not admin.locator("details.add-panel[open]").count():
        admin.click(".add-summary")
    admin.fill("#key", "journey-flag")
    admin.fill("#description", "A journey")
    admin.click("details.add-panel button[type=submit]")
    admin.wait_for_url("**/admin/flags/*")
    admin.check("#enabled")
    admin.fill("#rollout_pct", "10")
    admin.fill("#allow_users", ACCOUNT)
    admin.click("text=Save flag")
    admin.wait_for_url("**/admin/flags")
    expect(admin.locator("main tbody")).to_contain_text("10%")
    with e2e_app.app_context():
        flag = db.session.scalar(db.select(FeatureFlag).where(FeatureFlag.key == "journey-flag"))
        assert flag.enabled
        assert flag.rollout_pct == 10
        assert flags.flag_applies(flag, flag.allow_user_ids[0])
        db.session.remove()

    # TestSupportRoundTrip in test_behaviour.py follows a support message and its reply.
    customer = new_page(state=logged_in_state)
    customer.goto("/app/support")
    customer.fill("#subject", "From the journey")
    customer.fill("#body", "Hello")
    customer.click("main form button[type=submit]")
    admin.goto("/admin/support")
    admin.click("a:has-text('From the journey')")
    admin.fill("#reply", "Answered")
    admin.click("text=Send reply")
    customer.goto("/app/support")
    expect(customer.locator(".reply").first).to_contain_text("Answered")
