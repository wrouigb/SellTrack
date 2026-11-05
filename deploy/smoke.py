#!/usr/bin/env python3
"""Check that a deployed stack really works. If this fails after a deploy, the deploy rolls back.

    python deploy/smoke.py --base-url http://nginx:8080

Only what's in Python's standard library, so it needs nothing that could itself be broken.
Logging in is only checked when SMOKE_USERNAME and SMOKE_PASSWORD are set. A late backup is only a
warning: a new release didn't cause that, so going back to the old one wouldn't help.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT_SECONDS = 15
OK = 200
FOUND = 302
BACKUP_MAX_AGE = timedelta(hours=26)
SECURITY_HEADERS = (
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
)
STRICT_ENVIRONMENTS = ("staging", "production")
ASSET = re.compile(r'"(/static/dist/[^"]+\.(?:css|js))"')
CSRF = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
SELECT = r'<select[^>]*\bid="{}"[^>]*>(.*?)</select>'
OPTION = re.compile(r'<option[^>]*\bvalue="(\d+)"')
DELETE_BUTTON = re.compile(r'formaction="(/app/sales/\d+/delete)"')
SMOKE_CUSTOMER = "Smoke check"


class SmokeCheckError(Exception):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Browser:
    """Keeps cookies by hand. Cookies here are marked Secure, and this talks plain HTTP to Nginx."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies: dict[str, str] = {}
        self.opener = build_opener(_NoRedirects)

    def request(self, path: str, data: dict[str, str] | None = None):
        body = urlencode(data).encode() if data is not None else None
        request = Request(self.base_url + path, data=body)  # noqa: S310 - only ever our own URL
        if self.cookies:
            cookie = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
            request.add_header("Cookie", cookie)
        try:
            response = self.opener.open(request, timeout=TIMEOUT_SECONDS)
        except HTTPError as error:  # includes redirects, because they aren't being followed
            response = error
        with response:
            content = response.read().decode("utf-8", errors="replace")
        for header in response.headers.get_all("Set-Cookie") or ():
            parsed = SimpleCookie()
            parsed.load(header)
            for name, morsel in parsed.items():
                self.cookies[name] = morsel.value
        return response.status, response.headers, content


def expect(condition: object, message: str) -> None:
    if not condition:
        raise SmokeCheckError(message)


def check_health(browser: Browser) -> None:
    status, _headers, _body = browser.request("/healthz")
    expect(status == OK, f"/healthz returned {status}")
    status, _headers, body = browser.request("/readyz")
    expect(status == OK, f"/readyz returned {status}: {body}")


def check_landing_page(browser: Browser, *, cached_assets: bool) -> None:
    status, headers, body = browser.request("/")
    expect(status == OK, f"the landing page returned {status}")
    for name in SECURITY_HEADERS:
        expect(headers.get(name), f"the landing page has no {name} header")
    if os.environ.get("APP_ENV") in STRICT_ENVIRONMENTS:
        expect(headers.get("Strict-Transport-Security"), "the landing page has no HSTS header")

    assets = set(ASSET.findall(body))
    expect(len(assets) >= 2, f"the landing page should load CSS and JavaScript, found {assets}")
    for asset in assets:
        status, headers, _body = browser.request(asset)
        expect(status == OK, f"{asset} returned {status}")
        if cached_assets:
            cache = headers.get("Cache-Control", "")
            expect("immutable" in cache, f"{asset} has the wrong Cache-Control: {cache!r}")


def _token(body: str, where: str) -> str:
    match = CSRF.search(body)
    expect(match, f"no CSRF token on {where}")
    return match.group(1)


def _first_option(body: str, select_id: str) -> str:
    select = re.search(SELECT.format(select_id), body, re.DOTALL)
    expect(select, f"no {select_id} on the sale form")
    option = OPTION.search(select.group(1))
    expect(option, f"nothing to choose in {select_id} on the sale form")
    return option.group(1)


def _sales_to_delete(browser: Browser) -> list[str]:
    query = urlencode({"q": SMOKE_CUSTOMER})
    status, _headers, body = browser.request(f"/app/sales?{query}")
    expect(status == OK, f"the sales list returned {status}")
    return sorted(set(DELETE_BUTTON.findall(body)))


def check_login_and_sale(browser: Browser, username: str, password: str) -> None:
    _status, _headers, body = browser.request("/login")
    status, headers, _body = browser.request(
        "/login",
        {"username": username, "password": password, "csrf_token": _token(body, "the login page")},
    )
    expect(status == FOUND, f"logging in returned {status}")
    expect(urlsplit(headers.get("Location", "")).path == "/app", "logging in went somewhere odd")

    status, _headers, body = browser.request("/app/sales/new")
    expect(status == OK, f"the sale form returned {status}")
    status, _headers, _body = browser.request(
        "/app/sales/new",
        {
            "csrf_token": _token(body, "the sale form"),
            "product_id": _first_option(body, "product_id"),
            "sale_date": datetime.now(UTC).date().isoformat(),
            "quantity": "1",
            "price_paid": "1.00",
            "customer_name": SMOKE_CUSTOMER,
        },
    )
    expect(status == FOUND, f"saving a sale returned {status}")

    # Including any left behind by a check that stopped partway through.
    found = _sales_to_delete(browser)
    expect(found, "the sale that was just saved isn't in the list")
    _status, _headers, body = browser.request(f"/app/sales?{urlencode({'q': SMOKE_CUSTOMER})}")
    token = _token(body, "the sales list")
    for delete_path in found:
        status, _headers, _body = browser.request(delete_path, {"csrf_token": token})
        expect(status == FOUND, f"deleting a sale returned {status}")
    expect(not _sales_to_delete(browser), "the sale was deleted, but it's still in the list")


def warn_about_backups(folder: str | None) -> None:
    if not folder:
        return
    path = Path(folder)
    files = [f for f in path.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        print("warning: there are no backups yet")
        return
    newest = max(file.stat().st_mtime for file in files)
    age = datetime.now(UTC) - datetime.fromtimestamp(newest, UTC)
    if age > BACKUP_MAX_AGE:
        print(f"warning: the last backup is {age} old")


def run(base_url: str, *, cached_assets: bool, retries: int, wait_seconds: float) -> None:
    # Nginx may still be finding the app, in the first moments after a restart.
    browser = Browser(base_url)
    for attempt in range(retries + 1):
        try:
            check_health(browser)
            break
        except (SmokeCheckError, URLError, OSError) as error:
            if attempt == retries:
                raise SmokeCheckError(f"never became healthy: {error}") from error
            time.sleep(wait_seconds)
    print("ok: /healthz and /readyz")

    check_landing_page(browser, cached_assets=cached_assets)
    print("ok: the landing page, its security headers and its assets")

    username = os.environ.get("SMOKE_USERNAME")
    password = os.environ.get("SMOKE_PASSWORD")
    if username and password:
        check_login_and_sale(Browser(base_url), username, password)
        print("ok: logged in, added a sale and deleted it")
    else:
        print("warning: SMOKE_USERNAME and SMOKE_PASSWORD aren't set, so login wasn't checked")

    warn_about_backups(os.environ.get("BACKUP_DIR"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--no-cache-check",
        action="store_true",
        help="don't check for long-cache headers, for testing without Nginx",
    )
    parser.add_argument("--retries", type=int, default=20)
    parser.add_argument("--wait-seconds", type=float, default=3.0)
    args = parser.parse_args()
    try:
        run(
            args.base_url,
            cached_assets=not args.no_cache_check,
            retries=args.retries,
            wait_seconds=args.wait_seconds,
        )
    except (SmokeCheckError, URLError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    print("All good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
