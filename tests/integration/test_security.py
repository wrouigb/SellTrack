"""CSRF, cookies, headers, forged sessions and proxies (plan section 2.4)."""

from __future__ import annotations

import re

import pytest
from flask.sessions import SecureCookieSessionInterface
from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models import User
from app.services import auth
from tests.conftest import csrf_token_from, make_settings, register
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

SECRET = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3Bx"
LEGACY_SECRET = "change-this-to-your-own-secret-text"


@pytest.fixture
def build_app():
    """Build a separate app, then put the shared rate limiter back as the main app expects."""

    def build(**kwargs):
        overrides = kwargs.pop("overrides", None)
        return create_app(make_settings(**kwargs), overrides=overrides)

    yield build
    create_app(make_settings())


class TestCsrf:
    """Audit issue 3: no POST form had CSRF protection."""

    @pytest.mark.parametrize(
        "path",
        [
            "/login",
            "/register",
            "/logout",
            "/recover",
            "/app/settings/security/password",
            "/app/settings/security/recovery-codes",
            "/app/settings/security/logout-everywhere",
        ],
    )
    def test_post_without_a_token_is_rejected(self, csrf_client, path):
        response = csrf_client.post(path, data={"username": "alex", "password": PASSWORD})
        assert response.status_code == 400
        assert "try that again" in response.text

    def test_every_route_that_changes_data_is_covered(self, app):
        """A new POST route can't be forgotten: CSRF is on globally, with no exemptions."""
        csrf = app.extensions["csrf"]
        assert csrf._exempt_views == set()
        assert csrf._exempt_blueprints == set()
        assert app.config.get("WTF_CSRF_CHECK_DEFAULT", True) is True

    def test_post_with_the_token_works(self, csrf_client):
        token = csrf_token_from(csrf_client.get("/register").text)
        response = csrf_client.post(
            "/register",
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "csrf_token": token,
            },
        )
        assert response.status_code == 200
        assert csrf_client.get("/app").status_code == 200

    def test_token_from_another_session_is_rejected(self, csrf_client, app):
        other = csrf_client.application.test_client()
        token = csrf_token_from(other.get("/register").text)
        csrf_client.get("/register")
        response = csrf_client.post(
            "/register",
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "csrf_token": token,
            },
        )
        assert response.status_code == 400

    def test_logged_in_pages_include_a_working_token_for_logout(self, csrf_client):
        token = csrf_token_from(csrf_client.get("/register").text)
        csrf_client.post(
            "/register",
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "csrf_token": token,
            },
        )
        page = csrf_client.get("/app")
        response = csrf_client.post("/logout", data={"csrf_token": csrf_token_from(page.text)})
        assert response.status_code == 302
        assert csrf_client.get("/app").status_code == 302


class TestHeaders:
    """Audit issue 7: no security headers were set."""

    @pytest.mark.parametrize("path", ["/", "/login", "/healthz", "/no-such-page"])
    def test_on_every_response(self, client, path):
        headers = client.get(path).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in headers["Permissions-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_csp_allows_nothing_inline_or_external(self, client):
        csp = client.get("/").headers["Content-Security-Policy"]
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
        assert "http:" not in csp
        assert "https:" not in csp
        assert "*" not in csp
        assert "default-src 'none'" in csp
        assert "base-uri 'none'" in csp
        assert "form-action 'self'" in csp

    def test_csp_nonce_is_different_every_time(self, client):
        first = client.get("/").headers["Content-Security-Policy"]
        second = client.get("/").headers["Content-Security-Policy"]
        assert first != second

    def test_pages_have_no_inline_scripts_or_handlers(self, client):
        for path in ("/", "/help", "/login", "/register", "/recover", "/no-such-page"):
            html = client.get(path).text
            scripts = re.findall(r"<script\b[^>]*>", html)
            assert scripts, "the page should load the app's script"
            assert all(" src=" in script for script in scripts), scripts
            assert not re.search(r"\son[a-z]+=", html), "no inline event handlers"
            assert ' style="' not in html, "no inline styles"

    def test_logged_in_pages_are_not_cached(self, client):
        assert "Cache-Control" not in client.get("/login").headers
        register(client, "alex", PASSWORD)
        assert client.get("/app").headers["Cache-Control"] == "no-store"

    def test_no_hsts_outside_https_environments(self, client):
        assert "Strict-Transport-Security" not in client.get("/").headers


class TestProductionCookies:
    """Audit issue 5: cookies weren't Secure."""

    @pytest.fixture
    def production(self, build_app):
        return build_app(app_env="production", secret_key=SECRET)

    def test_hsts_is_on(self, production):
        response = production.test_client().get("/", base_url="https://selltrack.example")
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]

    def test_session_cookie_is_locked_down(self, production):
        client = production.test_client()
        page = client.get("/register", base_url="https://selltrack.example")
        cookie = page.headers["Set-Cookie"]
        assert cookie.startswith("__Host-selltrack_session=")
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Path=/" in cookie
        assert "Domain" not in cookie

    def test_full_login_over_https_with_csrf_on(self, production):
        client = production.test_client()
        base = "https://selltrack.example"
        token = csrf_token_from(client.get("/register", base_url=base).text)
        response = client.post(
            "/register",
            base_url=base,
            headers={"Referer": f"{base}/register"},
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "csrf_token": token,
            },
        )
        assert response.status_code == 200
        assert client.get("/app", base_url=base).status_code == 200

    def test_csrf_rejects_a_post_from_another_site(self, production):
        client = production.test_client()
        base = "https://selltrack.example"
        token = csrf_token_from(client.get("/register", base_url=base).text)
        response = client.post(
            "/register",
            base_url=base,
            headers={"Referer": "https://evil.example/page"},
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "csrf_token": token,
            },
        )
        assert response.status_code == 400


class TestForgedSessions:
    """Audit issue 1: anyone could forge a cookie using the hard-coded fallback secret."""

    def _cookie_signed_with(self, app, secret, value):
        serializer = SecureCookieSessionInterface().get_signing_serializer(
            _FakeApp(secret, app.config)
        )
        return serializer.dumps(value)

    def _make_user(self):
        account = auth.register("alex", PASSWORD)
        return account.user

    def test_cookie_signed_with_the_old_fallback_secret_is_rejected(self, app, ctx):
        user = self._make_user()
        forged = self._cookie_signed_with(
            app, LEGACY_SECRET, {"_user_id": user.get_id(), "_fresh": True}
        )
        client = app.test_client()
        client.set_cookie("selltrack_session", forged)
        assert client.get("/app").status_code == 302

    def test_cookie_signed_with_the_real_secret_works(self, app, ctx):
        """Confirms the forgery test above is rejecting for the right reason."""
        user = self._make_user()
        real = self._cookie_signed_with(
            app, app.config["SECRET_KEY"], {"_user_id": user.get_id(), "_fresh": True}
        )
        client = app.test_client()
        client.set_cookie("selltrack_session", real)
        assert client.get("/app").status_code == 200

    def test_tampered_cookie_is_rejected(self, app, client):
        register(client, "alex", PASSWORD)
        value = client.get_cookie("selltrack_session").value
        tampered = ("A" if value[0] != "A" else "B") + value[1:]
        attacker = app.test_client()
        attacker.set_cookie("selltrack_session", tampered)
        assert attacker.get("/app").status_code == 302

    @pytest.mark.parametrize("value", ["1", "1:", ":1", "abc:1", "1:abc", "", "1:1:1", "99999:1"])
    def test_malformed_session_values_are_logged_out_not_errors(self, app, ctx, value):
        self._make_user()
        cookie = self._cookie_signed_with(app, app.config["SECRET_KEY"], {"_user_id": value})
        client = app.test_client()
        client.set_cookie("selltrack_session", cookie)
        assert client.get("/app").status_code == 302


class _FakeApp:
    """Just enough of a Flask app to make Flask's own cookie signer use a chosen secret."""

    def __init__(self, secret, config):
        self.secret_key = secret
        self.config = config


class TestProxies:
    """Rate limits and sign-in history depend on knowing the real client address."""

    def test_forwarded_address_is_ignored_when_no_proxy_is_trusted(self, app):
        with app.test_request_context(
            "/",
            headers={"X-Forwarded-For": "203.0.113.9"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        ):
            from flask import request

            assert request.remote_addr == "10.0.0.1"

    def test_spoofing_the_header_cannot_dodge_the_rate_limit(self, app, client):
        auth_args = {"username": "nobody", "password": "not-the-password"}
        for number in range(20):
            response = client.post(
                "/login",
                data={**auth_args, "username": f"user{number}"},
                headers={"X-Forwarded-For": f"203.0.113.{number}"},
            )
            assert response.status_code == 401
        blocked = client.post("/login", data=auth_args, headers={"X-Forwarded-For": "203.0.113.99"})
        assert blocked.status_code == 429

    def test_real_address_comes_from_the_trusted_proxy(self, build_app, clean_state):
        application = build_app(trusted_proxy_count=1)
        client = application.test_client()
        client.post(
            "/register",
            data={"username": "alex", "password": PASSWORD, "confirm": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        client.post("/logout")
        client.post(
            "/login",
            data={"username": "alex", "password": PASSWORD},
            headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"},
        )
        with application.app_context():
            from app.models import SignIn

            sign_in = db.session.scalar(select(SignIn))
            # A client can add anything to the left; only the proxy's own entry counts.
            assert str(sign_in.ip) == "203.0.113.9"


class TestRateLimitStorage:
    def test_logins_keep_working_and_limited_when_redis_is_down(self, build_app, clean_state):
        application = build_app(redis_url="redis://localhost:1/0")
        client = application.test_client()
        statuses = [
            client.post(
                "/login", data={"username": "alex", "password": "not-the-password"}
            ).status_code
            for _ in range(7)
        ]
        assert statuses[:5] == [401] * 5, "Redis being down must not lock everyone out"
        assert statuses[5:] == [429, 429], "attempts are still limited, counted in memory"


class TestMassAssignment:
    def test_form_fields_cannot_set_internal_columns(self, app, client):
        client.post(
            "/register",
            data={
                "username": "alex",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "session_version": "99",
                "status": "suspended",
                "must_change_password": "true",
                "id": "1000",
            },
        )
        with app.app_context():
            user = db.session.scalar(select(User))
            assert user.id != 1000
            assert user.session_version == 1
            assert user.is_active
            assert not user.must_change_password
