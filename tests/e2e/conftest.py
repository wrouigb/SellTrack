"""Browser tests: a real Chromium against the app running on real Postgres and Redis.

They need built assets (``npm run build``) and a browser (``uv run playwright install chromium``).
Set ``E2E_CHROMIUM_PATH`` to use a particular Chromium instead of the one Playwright installed.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import redis
import sqlalchemy as sa
from flask import Flask
from playwright.sync_api import Browser, Page, Playwright, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db
from app.services import auth, products, sales, split
from tests.conftest import make_settings, redis_url
from tests.factories import PASSWORD

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "app/static/dist/manifest.json"
AXE = ROOT / "node_modules/axe-core/axe.min.js"

ACCOUNT = "markets"
EMPTY_ACCOUNT = "newcomer"
PHONE = {"width": 360, "height": 740}
DESKTOP = {"width": 1280, "height": 860}


@pytest.fixture(scope="session")
def built_assets() -> None:
    if not MANIFEST.exists():
        pytest.fail("Assets aren't built. Run `npm run build` first.", pytrace=False)
    if not AXE.exists():
        pytest.fail("axe-core isn't installed. Run `npm ci` first.", pytrace=False)


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    instance = playwright_instance.chromium.launch(
        executable_path=os.environ.get("E2E_CHROMIUM_PATH")
    )
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def e2e_app(app: Flask, built_assets: None) -> Flask:
    """A separate app with CSRF on, exactly as in production."""
    return create_app(
        make_settings(),
        overrides={"WTF_CSRF_ENABLED": True, "RATELIMIT_ENABLED": True},
    )


@pytest.fixture(scope="session")
def data(e2e_app: Flask) -> dict[str, Any]:
    """A realistic account, and one that hasn't done anything yet."""
    with e2e_app.app_context():
        tables = ", ".join(f'"{table.name}"' for table in db.metadata.sorted_tables)
        with db.engine.begin() as connection:
            connection.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        redis.Redis.from_url(redis_url()).flushdb()

        account = auth.register(ACCOUNT, PASSWORD)
        uid = account.user.id
        auth.complete_onboarding(account.user)
        alex = split.add_person(uid, name="Alex", share_pct="50")
        sam = split.add_person(uid, name="Sam", share_pct="50")
        third = split.add_person(uid, name="Former partner", share_pct="0")
        stamp = products.create_product(uid, name="Stamp set", cost="6.50", sell_price="24")
        ink = products.create_product(uid, name="Ink pad", cost="2.10", sell_price="7.50")
        products.create_product(uid, name="Canvas tote", cost="9.00")
        archived = products.create_product(uid, name="Old product", cost="1.00")
        products.archive_product(uid, archived.id)

        def sale(product, day, price, who):
            return sales.create_sale(
                uid,
                product_id=product.id,
                sale_date=day,
                quantity=1,
                price_paid=price,
                customer_name="Pat",
                received_by_partner_id=who.id,
            )

        settled_sale = sale(stamp, "2026-10-04", "24", sam)
        settled_cost = split.add_cost(
            uid,
            description="October stall",
            category="fees",
            amount="10",
            cost_date="2026-10-03",
            paid_by_partner_id=sam.id,
        )
        settled_payment = split.record_payment(
            uid, from_partner_id=sam.id, to_partner_id=alex.id, amount="1", paid_on="2026-10-05"
        )
        settlement = split.settle_up(uid, actor_id=uid)
        split.archive_person(uid, third.id)

        open_sale = sale(ink, "2026-11-01", "7.50", sam)
        sale(stamp, "2026-11-02", "24", alex)
        for day in range(3, 30):
            sale(ink, f"2026-11-{day:02d}", "7.50", sam)
        open_cost = split.add_cost(
            uid,
            description="Rubber and blocks",
            category="stock",
            amount="35",
            cost_date="2026-11-01",
            paid_by_partner_id=alex.id,
        )
        open_payment = split.record_payment(
            uid, from_partner_id=sam.id, to_partner_id=alex.id, amount="5", paid_on="2026-11-03"
        )
        records = {
            "user": uid,
            "product": stamp.id,
            "settled_sale": settled_sale.id,
            "open_sale": open_sale.id,
            "settled_cost": settled_cost.id,
            "open_cost": open_cost.id,
            "settled_payment": settled_payment.id,
            "open_payment": open_payment.id,
            "settlement": settlement.id,
            "codes": account.recovery_codes,
        }
        auth.register(EMPTY_ACCOUNT, PASSWORD)
        db.session.remove()
    return records


@pytest.fixture(scope="session")
def base_url(e2e_app: Flask, data: dict[str, Any]) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, e2e_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()


def _storage_state(browser: Browser, base_url: str, username: str, path: Path) -> str:
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base_url}/login")
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.click("main button[type=submit]")
    page.wait_for_url(f"{base_url}/app")
    context.storage_state(path=str(path))
    context.close()
    return str(path)


@pytest.fixture(scope="session")
def logged_in_state(browser: Browser, base_url: str, tmp_path_factory: Any) -> str:
    return _storage_state(
        browser, base_url, ACCOUNT, tmp_path_factory.mktemp("state") / "markets.json"
    )


@pytest.fixture(scope="session")
def empty_state(browser: Browser, base_url: str, tmp_path_factory: Any) -> str:
    return _storage_state(
        browser, base_url, EMPTY_ACCOUNT, tmp_path_factory.mktemp("state") / "newcomer.json"
    )


@pytest.fixture
def new_page(browser: Browser, base_url: str) -> Iterator[Any]:
    """Make a page in a fresh browser context, collecting anything the browser complains about."""
    contexts = []
    pages = []

    def make(
        *,
        state: str | None = None,
        viewport: dict[str, int] = DESKTOP,
        dark: bool = False,
        javascript: bool = True,
    ) -> Page:
        context = browser.new_context(
            base_url=base_url,
            storage_state=state,
            viewport=viewport,
            color_scheme="dark" if dark else "light",
            java_script_enabled=javascript,
        )
        contexts.append(context)
        page = context.new_page()
        page.problems = []  # type: ignore[attr-defined]
        pages.append(page)

        def on_console(message: Any) -> None:
            if message.type == "error" and "status of 4" not in message.text:
                page.problems.append(message.text)  # type: ignore[attr-defined]

        page.on("console", on_console)
        page.on("pageerror", lambda error: page.problems.append(str(error)))  # type: ignore[attr-defined]
        return page

    yield make
    problems = [problem for page in pages for problem in page.problems]
    for context in contexts:
        context.close()
    # Any JavaScript error or blocked resource fails the test that caused it.
    assert not problems, problems


def axe_violations(page: Page, within: str | None = None) -> list[str]:
    """Run axe-core on the page, or part of it, and describe anything it finds."""
    page.evaluate(AXE.read_text())
    results = page.evaluate(
        """async (within) => {
            const results = await axe.run(within || document, {
                runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'],
            });
            return results.violations.map((v) => ({
                id: v.id,
                impact: v.impact,
                nodes: v.nodes.slice(0, 3).map((n) => n.target.join(' ') + ': ' + n.failureSummary),
            }));
        }""",
        within,
    )
    return [f"{v['id']} ({v['impact']}): {v['nodes']}" for v in results]
