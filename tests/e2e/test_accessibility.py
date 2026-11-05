"""Phase 4's done-when: axe and keyboard checks clean on every page, and usable at 360px."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import DESKTOP, PHONE, axe_violations

pytestmark = pytest.mark.e2e

GUEST_PAGES = [
    "/",
    "/help",
    "/privacy",
    "/terms",
    "/login",
    "/register",
    "/recover",
    "/no-such-page",
]
APP_PAGES = [
    "/app",
    "/app?period=all_time",
    "/app?period=custom",
    "/app/sales",
    "/app/sales?q=nothing-matches-this",
    "/app/sales/new",
    "/app/sales/{open_sale}/edit",
    "/app/sales/{settled_sale}/edit",
    "/app/products",
    "/app/products?archived=y",
    "/app/products/{product}",
    "/app/split",
    "/app/split/settle",
    "/app/split/costs",
    "/app/split/costs/{open_cost}/edit",
    "/app/split/costs/{settled_cost}/edit",
    "/app/split/payments",
    "/app/split/payments/{open_payment}/edit",
    "/app/split/payments/{settled_payment}/edit",
    "/app/split/history",
    "/app/split/history/{settlement}",
    "/app/split/people",
    "/app/settings",
    "/app/settings/security",
    "/app/settings/security/password",
    "/app/settings/preferences",
    "/app/settings/data",
    "/onboarding",
    "/onboarding/people",
    "/onboarding/product",
    "/onboarding/sale",
]
EMPTY_ACCOUNT_PAGES = [
    "/app",
    "/app/sales",
    "/app/sales/new",
    "/app/products",
    "/app/split",
    "/app/split/costs",
    "/app/split/payments",
    "/app/split/history",
    "/app/split/people",
    "/onboarding/sale",
]


def _fill(path, data):
    return path.format(**data)


def _check(page, problems_allowed=False):
    violations = axe_violations(page)
    assert not violations, "\n".join(violations)
    if not problems_allowed:
        assert not page.problems, page.problems
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"the page scrolls sideways by {overflow}px"


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
class TestEveryPage:
    @pytest.mark.parametrize("path", GUEST_PAGES)
    def test_guest_pages(self, new_page, path, dark, viewport):
        page = new_page(viewport=viewport, dark=dark)
        page.goto(path)
        _check(page)

    @pytest.mark.parametrize("path", APP_PAGES)
    def test_app_pages(self, new_page, logged_in_state, data, path, dark, viewport):
        page = new_page(state=logged_in_state, viewport=viewport, dark=dark)
        page.goto(_fill(path, data))
        _check(page)

    @pytest.mark.parametrize("path", EMPTY_ACCOUNT_PAGES)
    def test_empty_account_pages(self, new_page, empty_state, path, dark, viewport):
        page = new_page(state=empty_state, viewport=viewport, dark=dark)
        page.goto(path)
        _check(page)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
class TestStatesThatOnlyAppearAfterSomething:
    def test_login_error(self, new_page, dark, viewport):
        page = new_page(viewport=viewport, dark=dark)
        page.goto("/login")
        page.fill("#username", "nobody-here")
        page.fill("#password", "wrong-password")
        page.click("main button[type=submit]")
        page.wait_for_selector("text=Wrong username or password")
        _check(page)

    def test_form_with_errors(self, new_page, logged_in_state, dark, viewport):
        page = new_page(state=logged_in_state, viewport=viewport, dark=dark)
        page.goto("/app/sales/new")
        page.fill("#price_paid", "NaN")
        page.click("main form button[type=submit] >> nth=0")
        page.wait_for_selector(".error-summary")
        _check(page)

    def test_open_add_forms(self, new_page, logged_in_state, dark, viewport):
        page = new_page(state=logged_in_state, viewport=viewport, dark=dark)
        for path in (
            "/app/products",
            "/app/split/costs",
            "/app/split/payments",
            "/app/split/people",
        ):
            page.goto(path)
            page.click(".add-summary")
            _check(page)

    def test_chart_table_open(self, new_page, logged_in_state, dark, viewport):
        page = new_page(state=logged_in_state, viewport=viewport, dark=dark)
        page.goto("/app?period=all_time")
        page.click("details.more summary")
        _check(page)

    def test_account_menu_open(self, new_page, logged_in_state, dark, viewport):
        page = new_page(state=logged_in_state, viewport=viewport, dark=dark)
        page.goto("/app")
        page.click("details.menu summary")
        # Only the menu: an open menu covers what's under it, which is what menus are for.
        assert not axe_violations(page, within="details.menu")


class TestRealRegistrationPage:
    """The recovery codes page can only be reached by creating an account."""

    def test_recovery_codes_page(self, new_page):
        page = new_page()
        page.goto("/register")
        page.fill("#username", "codes-page-check")
        page.fill("#password", "correct-horse-battery")
        page.fill("#confirm", "correct-horse-battery")
        page.click("main button[type=submit]")
        page.wait_for_selector("text=Save your recovery codes")
        _check(page)
        assert page.locator(".codes code").count() == 10


class TestKeyboard:
    """Everything can be reached and used without a mouse, and you can see where you are."""

    def _walk(self, page, stops=60):
        """Tab through the page, returning every element reached. Checks each one is visible."""
        reached = []
        for _ in range(stops):
            page.keyboard.press("Tab")
            info = page.evaluate(
                """() => {
                    const el = document.activeElement;
                    if (!el || el === document.body) return null;
                    const style = getComputedStyle(el);
                    const box = el.getBoundingClientRect();
                    return {
                        tag: el.tagName,
                        text: (el.innerText || el.getAttribute('aria-label') || el.id || '')
                            .trim()
                            .slice(0, 40),
                        outline: style.outlineStyle !== 'none'
                            && parseFloat(style.outlineWidth) >= 2,
                        visible: box.width > 0 && box.height > 0,
                        width: box.width,
                        height: box.height,
                    };
                }"""
            )
            if info is None:
                break
            reached.append(info)
        return reached

    @pytest.mark.parametrize(
        "path",
        [
            "/app",
            "/app/sales",
            "/app/sales/new",
            "/app/products",
            "/app/split",
            "/app/split/people",
            "/app/settings/security",
        ],
    )
    @pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
    def test_every_stop_has_a_visible_focus_ring_and_a_big_enough_target(
        self, new_page, logged_in_state, path, viewport
    ):
        page = new_page(state=logged_in_state, viewport=viewport)
        page.goto(path)
        reached = self._walk(page)
        assert reached, "tabbing reached nothing"
        for stop in reached:
            assert stop["visible"], stop
            assert stop["outline"], f"no visible focus ring: {stop}"

    @pytest.mark.parametrize("path", ["/", "/login", "/app"])
    def test_the_skip_link_comes_first_and_works(self, new_page, logged_in_state, path):
        page = new_page(state=None if path != "/app" else logged_in_state)
        page.goto(path)
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.className") == "skip-link"
        box = page.evaluate("document.activeElement.getBoundingClientRect().top")
        assert box >= 0, "the skip link must become visible when focused"
        page.keyboard.press("Enter")
        assert page.evaluate("document.activeElement.id") == "main"

    def test_can_add_a_sale_with_only_the_keyboard(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app/sales/new")
        page.focus("#product_id")
        page.select_option("#product_id", label="Stamp set")
        page.keyboard.press("Tab")  # date
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") in ("quantity", "sale_date", "price_paid")
        page.focus("#customer_name")
        page.keyboard.type("Keyboard only")
        page.focus("main form button[type=submit] >> nth=0")
        page.keyboard.press("Enter")
        page.wait_for_url("**/app/sales")
        assert page.locator("text=Sale saved.").count() == 1

    def test_the_account_menu_opens_and_closes_with_the_keyboard(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app")
        page.focus("details.menu summary")
        page.keyboard.press("Enter")
        assert page.evaluate("document.querySelector('details.menu').open") is True
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.innerText") == "Settings"
        page.keyboard.press("Escape")
        assert page.evaluate("document.querySelector('details.menu').open") is False
        assert page.evaluate("document.activeElement.tagName") == "SUMMARY"

    def test_the_confirmation_dialog_traps_focus_and_escape_cancels(
        self, new_page, logged_in_state, data
    ):
        page = new_page(state=logged_in_state)
        page.goto(f"/app/products/{data['product']}")
        page.click("text=Delete product")
        assert page.evaluate("document.getElementById('confirm-dialog').open") is True
        focused_inside = page.evaluate(
            "document.getElementById('confirm-dialog').contains(document.activeElement)"
        )
        assert focused_inside
        assert "Stamp set" in page.inner_text("#confirm-dialog")
        page.keyboard.press("Escape")
        assert page.evaluate("document.getElementById('confirm-dialog').open") is False
        assert page.url.endswith(f"/app/products/{data['product']}"), "nothing was deleted"

    def test_the_chart_can_be_read_with_arrow_keys(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state)
        page.goto("/app?period=all_time")
        page.focus(".chart")
        assert page.is_hidden(".tooltip")
        page.keyboard.press("ArrowRight")
        assert page.is_visible(".tooltip")
        first = page.inner_text(".tooltip")
        page.keyboard.press("ArrowRight")
        assert page.inner_text(".tooltip") != first
        page.keyboard.press("Escape")
        assert page.is_hidden(".tooltip")


class TestPhoneLayout:
    def test_main_actions_are_big_enough_to_tap(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state, viewport=PHONE)
        page.goto("/app")
        sizes = page.evaluate(
            """() => [...document.querySelectorAll('.fab, .tabbar a, .brand, details.menu summary')]
                .map((el) => [el.innerText, el.getBoundingClientRect().height])"""
        )
        assert sizes
        for name, height in sizes:
            assert height >= 44, f"{name} is {height}px tall"

    def test_the_bottom_bar_and_add_sale_button_show_on_phones_only(
        self, new_page, logged_in_state
    ):
        phone = new_page(state=logged_in_state, viewport=PHONE)
        phone.goto("/app")
        assert phone.is_visible(".tabbar")
        assert phone.is_visible(".fab")
        assert phone.is_hidden(".nav-links")
        assert phone.is_hidden(".band-sale")

        desktop = new_page(state=logged_in_state, viewport=DESKTOP)
        desktop.goto("/app")
        assert desktop.is_hidden(".tabbar")
        assert desktop.is_hidden(".fab")
        assert desktop.is_visible(".nav-links")
        assert desktop.is_visible(".band-sale")

    def test_nothing_is_hidden_behind_the_bottom_bar(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state, viewport=PHONE)
        page.goto("/app/split/people")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        last = page.evaluate(
            "document.querySelector('main').lastElementChild.getBoundingClientRect().bottom"
        )
        bar = page.evaluate("document.querySelector('.tabbar').getBoundingClientRect().top")
        fab = page.evaluate("document.querySelector('.fab').getBoundingClientRect().top")
        assert last <= bar or last <= fab or last <= PHONE["height"]

    def test_all_split_tabs_are_in_view(self, new_page, logged_in_state):
        page = new_page(state=logged_in_state, viewport=PHONE)
        page.goto("/app/split/people")
        boxes = page.evaluate(
            "[...document.querySelectorAll('.tab')].map((t) => t.getBoundingClientRect().right)"
        )
        assert len(boxes) == 5
        assert all(right <= PHONE["width"] for right in boxes)
