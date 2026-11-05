"""The things JavaScript adds, that no server-side test can see."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import DESKTOP
from tests.factories import PASSWORD

pytestmark = pytest.mark.e2e


class TestSaleForm:
    @pytest.fixture
    def form(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales/new")
        return page

    def test_price_fills_in_and_profit_shows_as_you_type(self, form):
        form.select_option("#product_id", label="Stamp set")
        assert form.input_value("#price_paid") == "24.00"
        assert form.inner_text(".profit-preview strong") == "$17.50"

        form.fill("#quantity", "3")
        assert form.input_value("#price_paid") == "72.00"
        assert form.inner_text(".profit-preview strong") == "$52.50"

    def test_your_own_price_is_not_overwritten(self, form):
        form.select_option("#product_id", label="Stamp set")
        form.fill("#price_paid", "20")
        form.fill("#quantity", "2")
        assert form.input_value("#price_paid") == "20"
        assert form.inner_text(".profit-preview strong") == "$7.00"

    def test_clearing_the_price_hands_it_back_to_auto_fill(self, form):
        form.select_option("#product_id", label="Stamp set")
        form.fill("#price_paid", "20")
        form.fill("#price_paid", "")
        form.fill("#quantity", "2")
        assert form.input_value("#price_paid") == "48.00"

    def test_a_loss_is_shown_with_a_minus_sign_not_just_colour(self, form):
        form.select_option("#product_id", label="Stamp set")
        form.fill("#price_paid", "1.00")
        preview = form.locator(".profit-preview strong")
        assert preview.inner_text() == "−$5.50"
        assert "loss" in preview.get_attribute("class")

    def test_money_in_the_preview_is_exact(self, form):
        """With floats, 0.7 × 3 was 2.0999999999999996."""
        form.select_option("#product_id", label="Ink pad")
        form.fill("#quantity", "3")
        form.fill("#price_paid", "6.30")
        assert form.inner_text(".profit-preview strong") == "$0.00"

    @pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", "1e3", "1.234"])
    def test_nonsense_shows_no_preview(self, form, value):
        form.select_option("#product_id", label="Stamp set")
        form.fill("#price_paid", value)
        expect(form.locator(".profit-preview")).to_be_hidden()

    def test_a_product_with_no_selling_price_leaves_the_price_alone(self, form):
        form.select_option("#product_id", label="Canvas tote")
        assert form.input_value("#price_paid") == ""
        expect(form.locator(".profit-preview")).to_be_hidden()

    def test_the_usual_receiver_is_already_chosen(self, form):
        assert form.locator("#received_by_partner_id option:checked").inner_text() == "Alex"


class TestSalesListUpdates:
    def test_filtering_updates_results_and_address_without_a_reload(
        self, new_page, logged_in_state
    ):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales")
        page.evaluate("window.notReloaded = true")
        everything = page.inner_text("#result-count")
        page.fill("#q", "stamp")
        page.wait_for_url("**q=stamp**")
        expect(page.locator("#result-count")).not_to_have_text(everything)
        assert page.evaluate("window.notReloaded") is True, "the page shouldn't have reloaded"
        assert page.input_value("#q") == "stamp"
        assert page.evaluate("document.activeElement.id") == "q", "typing mustn't lose focus"
        assert "q=stamp" in page.get_attribute("#csv-link", "href"), (
            "the download follows the filter"
        )

    def test_sorting_and_paging_update_in_place(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales")
        page.evaluate("window.notReloaded = true")
        page.click("#sort-price")
        page.wait_for_url("**sort=price**")
        page.wait_for_selector('th[aria-sort="descending"] #sort-price')
        page.click("#page-next")
        page.wait_for_url("**page=2**")
        page.wait_for_selector("text=Page 2 of")
        assert page.evaluate("window.notReloaded") is True

    def test_sales_stay_out_of_browser_storage(self, new_page, logged_in_state):
        """htmx normally saves pages to localStorage, where a shared computer would keep them."""
        page = new_page(state=logged_in_state)
        page.goto("/app/sales")
        page.click("#sort-price")
        page.wait_for_url("**sort=price**")
        assert page.evaluate("JSON.stringify(localStorage)") in (
            "{}",
            '{"htmx-history-cache":"[]"}',
        )

    def test_back_button_works(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales")
        page.click("#sort-price")
        page.wait_for_url("**sort=price**")
        page.go_back()
        page.wait_for_function("!location.search.includes('sort=price')")
        page.wait_for_selector("#sales-results")


class TestBulkActions:
    def test_selection_bar_counts_and_select_all_works(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales")
        expect(page.locator(".bulk-bar")).to_be_hidden()
        page.locator("input[data-row]").first.check()
        expect(page.locator(".bulk-bar")).to_be_visible()
        assert page.inner_text(".bulk-bar span") == "1 sale selected"
        page.check("input[data-all]")
        page.wait_for_function(
            "document.querySelector('.bulk-bar span').innerText.includes('sales')"
        )
        assert (
            page.inner_text(".bulk-bar span")
            == f"{page.locator('input[data-row]').count()} sales selected"
        )
        page.uncheck("input[data-all]")
        expect(page.locator(".bulk-bar")).to_be_hidden()

    def test_bulk_delete_asks_first_then_can_be_undone(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales?q=stamp")
        before = page.inner_text("#result-count")
        page.locator("input[data-row]").first.check()
        page.click("text=Delete selected")
        assert page.evaluate("document.getElementById('confirm-dialog').open") is True
        page.click("#confirm-dialog [value=cancel]")
        assert page.inner_text("#result-count") == before, "cancelling deletes nothing"

        page.click("text=Delete selected")
        page.click("#confirm-dialog [data-accept]")
        page.wait_for_selector("text=1 sale deleted.")
        assert page.inner_text("#result-count") != before
        page.click("text=Undo")
        page.wait_for_selector("text=1 sale restored.")
        assert page.inner_text("#result-count") == before


class TestConfirmations:
    def test_a_nasty_name_is_shown_as_plain_text(self, new_page, logged_in_state):
        """Audit issue 6: this name ran as script in the original's inline confirm()."""
        page = new_page(state=logged_in_state)
        name = "x'); window.hacked = true; ('<img src=x onerror=window.hacked=true>"
        page.goto("/app/products")
        page.click(".add-summary")
        page.fill("#name", name)
        page.fill("#cost", "1")
        page.click("text=Add product")
        page.wait_for_selector("text=added.")
        page.click(f'a:has(span:text-is(" {name}"))')
        page.click("text=Delete product")
        message = page.inner_text("#confirm-dialog [data-message]")
        assert name in message
        assert page.evaluate("window.hacked") is None
        assert page.locator("#confirm-dialog img").count() == 0
        page.click("#confirm-dialog [data-accept]")
        page.wait_for_selector("text=Product deleted.")

    def test_double_clicking_only_submits_once(self, new_page, empty_state):
        page = new_page(state=empty_state)
        page.goto("/app/products")
        page.fill("#name", "Clicked twice")
        page.fill("#cost", "1")
        page.evaluate(
            """() => {
                const button = document.querySelector('main form[method=post] [type=submit]');
                button.click();
                button.click();
            }"""
        )
        page.wait_for_selector("text=Clicked twice added.")
        assert page.locator("text=already have a product").count() == 0


class TestPeoplePage:
    def test_shares_total_updates_as_you_type(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/split/people")
        assert page.inner_text(".share-total strong") == "100.00%"
        expect(page.locator(".share-total .badge")).to_be_hidden()
        page.locator("input[data-share]").first.fill("60")
        assert page.inner_text(".share-total strong") == "110.00%"
        expect(page.locator(".share-total .badge")).to_be_visible()
        page.locator("input[data-share]").first.fill("abc")
        assert "Check" in page.inner_text(".share-total strong")


class TestChart:
    def test_hovering_shows_every_value_for_that_day(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app?period=custom&date_from=2026-11-01&date_to=2026-11-05")
        page.wait_for_selector(".chart")
        band = page.locator(".chart-hit").nth(1)
        box = band.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        tip = page.inner_text(".tooltip")
        assert "2 November 2026" in tip
        assert "$24.00" in tip
        assert "$17.50" in tip
        page.mouse.move(0, 0)
        expect(page.locator(".tooltip")).to_be_hidden()

    def test_period_changes_without_a_reload(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app")
        page.evaluate("window.notReloaded = true")
        page.select_option("#period", "all_time")
        page.wait_for_url("**period=all_time**")
        page.wait_for_selector("#dashboard-body")
        assert page.evaluate("window.notReloaded") is True

    def test_choosing_dates_reveals_the_date_boxes(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app")
        expect(page.locator("#date_from")).to_be_hidden()
        page.select_option("#period", "custom")
        expect(page.locator("#date_from")).to_be_visible()
        expect(page.locator("#date_to")).to_be_visible()


class TestThemes:
    def _background(self, page):
        return page.evaluate("getComputedStyle(document.body).backgroundColor")

    def test_follows_the_device_by_default(self, new_page):
        light = new_page(dark=False)
        light.goto("/login")
        dark = new_page(dark=True)
        dark.goto("/login")
        assert self._background(light) == "rgb(245, 240, 230)"
        assert self._background(dark) == "rgb(18, 28, 39)"

    def test_a_chosen_theme_beats_the_device(self, new_page, empty_state):
        page = new_page(state=empty_state, dark=False)
        page.goto("/app/settings/preferences")
        page.select_option("#theme", "dark")
        page.click("main form button[type=submit]")
        page.wait_for_selector("text=Preferences saved.")
        assert self._background(page) == "rgb(18, 28, 39)"

        page.select_option("#theme", "light")
        page.click("main form button[type=submit]")
        page.wait_for_selector("text=Preferences saved.")
        dark_device = new_page(state=None, dark=True)
        assert dark_device is not None
        page.select_option("#theme", "system")
        page.click("main form button[type=submit]")
        page.wait_for_selector("text=Preferences saved.")


class TestWithoutJavaScript:
    """Every page works with JavaScript turned off."""

    def test_the_whole_daily_journey(self, new_page):
        page = new_page(javascript=False)
        page.goto("/register")
        page.fill("#username", "no-javascript")
        page.fill("#password", PASSWORD)
        page.fill("#confirm", PASSWORD)
        page.click("main button[type=submit]")
        page.click("text=I've saved them, continue")
        page.select_option("#timezone", "Australia/Sydney")
        page.click("text=Continue")
        page.fill("#name", "Ana")
        page.fill("#share_pct", "100")
        page.click("text=Add person")
        page.click("text=Continue")
        page.fill("#name", "Mug")
        page.fill("#cost", "4")
        page.fill("#sell_price", "12")
        page.click("text=Add product")
        page.fill("#price_paid", "12")
        page.click("main form button[type=submit] >> nth=0")
        page.wait_for_selector("text=You're all set.")

        page.goto("/app/sales")
        assert page.locator("text=Mug").count() >= 1
        page.fill("#q", "mug")
        page.click("text=Filter")
        assert "q=mug" in page.url
        page.click("tr button:has-text('Delete')")
        page.wait_for_selector("text=Sale deleted.")
        page.click("text=Undo")
        page.wait_for_selector("text=1 sale restored.")
        assert "q=mug" in page.url, "undo comes back to the same list"
        page.check("input[name=ids]")
        page.click("noscript button")
        page.wait_for_selector("text=1 sale deleted.")
        page.click("text=Undo")
        page.wait_for_selector("text=1 sale restored.")

        page.goto("/app?period=custom")
        expect(page.locator("#date_from")).to_be_visible()
        page.fill("#date_from", "2020-01-01")
        page.fill("#date_to", "2030-01-01")
        page.click("text=Show")
        assert "Mug" in page.inner_text("main")


class TestNoCspViolations:
    """Htmx and Alpine both have features the policy forbids. Nothing may trip it."""

    @pytest.mark.parametrize(
        "path",
        ["/", "/app", "/app/sales", "/app/sales/new", "/app/split", "/app/split/people"],
    )
    def test_nothing_is_blocked(self, new_page, logged_in_state, path):
        page = new_page(state=logged_in_state, viewport=DESKTOP)
        page.goto(path)
        page.wait_for_load_state("networkidle")
        assert not page.problems, page.problems

    def test_the_policy_is_really_on(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app")
        blocked = page.evaluate(
            """() => new Promise((resolve) => {
                document.addEventListener('securitypolicyviolation', () => resolve(true));
                const script = document.createElement('script');
                script.textContent = 'window.injected = true';
                document.body.appendChild(script);
                setTimeout(() => resolve(window.injected !== true), 300);
            })"""
        )
        assert blocked is True
        page.problems.clear()  # the one violation here was caused on purpose
