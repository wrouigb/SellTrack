"""Error pages, request IDs, structured logs and health checks."""

from __future__ import annotations

import io
import json
import logging

import pytest
import redis
import structlog
from flask import request
from sqlalchemy.exc import OperationalError

from app import create_app
from app.extensions import db
from app.services import sales
from app.services.errors import LockedError, NotFoundError
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def faulty_app():
    """An app with a route that blows up, showing what real users would see."""
    application = create_app(make_settings(), overrides={"PROPAGATE_EXCEPTIONS": False})

    @application.get("/boom")
    def boom():
        raise RuntimeError("secret detail: password=hunter2 at /srv/app/thing.py")

    @application.get("/boom-with-a-secret-nearby")
    def boom_with_a_secret_nearby():
        password = "local-variable-secret"
        raise RuntimeError(f"failed after reading {len(password)} characters")

    @application.get("/records/<int:record_id>")
    def record(record_id):
        return sales.get_sale(1, record_id).product_name

    @application.post("/tampered-form")
    def tampered_form():
        sales.delete_sales(1, [int(request.form["id"])])
        return "done"

    @application.get("/missing-record")
    def missing_record():
        raise NotFoundError("That sale doesn't exist.")

    @application.get("/locked-record")
    def locked_record():
        raise LockedError("This sale is part of a settle-up.")

    yield application
    create_app(make_settings())


@pytest.fixture
def log_output(app):
    """Collect what the app logs, using its real formatter."""
    root = logging.getLogger()
    formatter = next(
        h.formatter
        for h in root.handlers
        if isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    yield stream.getvalue
    root.removeHandler(handler)


def json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


class TestErrorPages:
    def test_404_is_friendly_and_has_a_reference(self, client):
        response = client.get("/no-such-page")
        assert response.status_code == 404
        assert "Page not found" in response.text
        assert response.headers["X-Request-ID"] in response.text

    def test_405(self, client):
        response = client.delete("/login")
        assert response.status_code == 405
        assert "<!doctype html>" in response.text

    def test_413_for_an_oversized_body(self, client):
        response = client.post("/login", data={"username": "x" * (2 * 1024 * 1024)})
        assert response.status_code == 413
        assert "Too much data" in response.text

    def test_429_page(self, client):
        for _ in range(5):
            client.post("/login", data={"username": "alex", "password": "not-the-password"})
        response = client.post("/login", data={"username": "alex", "password": "not-the-password"})
        assert response.status_code == 429
        assert "Too many attempts" in response.text

    def test_500_shows_only_a_reference(self, faulty_app):
        response = faulty_app.test_client().get("/boom")
        assert response.status_code == 500
        assert "Something went wrong" in response.text
        assert response.headers["X-Request-ID"] in response.text
        for leak in ("secret detail", "hunter2", "RuntimeError", "thing.py", "Traceback"):
            assert leak not in response.text

    def test_500_still_has_security_headers(self, faulty_app):
        response = faulty_app.test_client().get("/boom")
        assert "Content-Security-Policy" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_500_is_logged_with_the_traceback(self, faulty_app, log_output):
        response = faulty_app.test_client().get("/boom")
        output = log_output()
        assert "RuntimeError" in output
        assert response.headers["X-Request-ID"] in output

    def test_tracebacks_in_logs_leave_out_local_variables(self, faulty_app, log_output):
        faulty_app.test_client().get("/boom-with-a-secret-nearby")
        output = log_output()
        assert "RuntimeError" in output
        assert "local-variable-secret" not in output

    def test_json_clients_get_json(self, client):
        response = client.get("/no-such-page", headers={"Accept": "application/json"})
        assert response.status_code == 404
        assert response.json["error"] == "Page not found"
        assert response.json["reference"] == response.headers["X-Request-ID"]


class TestServiceErrors:
    def test_a_url_id_too_big_for_the_database_is_a_plain_404(self, faulty_app):
        client = faulty_app.test_client()
        assert client.get("/records/5").status_code == 404
        assert client.get(f"/records/{2**63 - 1}").status_code == 404
        assert client.get(f"/records/{2**63}").status_code == 404
        assert client.get(f"/records/{10**30}").status_code == 404

    def test_a_form_id_too_big_for_the_database_is_a_bad_request(self, faulty_app):
        client = faulty_app.test_client()
        response = client.post("/tampered-form", data={"id": str(10**30)})
        assert response.status_code == 400
        assert "bigint" not in response.text

    def test_a_record_that_is_not_yours_is_a_plain_404(self, faulty_app):
        response = faulty_app.test_client().get("/missing-record")
        assert response.status_code == 404
        assert "Page not found" in response.text

    def test_a_locked_record_says_why(self, faulty_app):
        response = faulty_app.test_client().get("/locked-record")
        assert response.status_code == 409
        assert "part of a settle-up" in response.text


class TestRequestIds:
    def test_generated_when_absent(self, client):
        assert len(client.get("/healthz").headers["X-Request-ID"]) == 36

    def test_different_every_time(self, client):
        assert (
            client.get("/healthz").headers["X-Request-ID"]
            != client.get("/healthz").headers["X-Request-ID"]
        )

    def test_a_sensible_inbound_id_is_kept(self, client):
        response = client.get("/healthz", headers={"X-Request-ID": "abc-12345"})
        assert response.headers["X-Request-ID"] == "abc-12345"

    @pytest.mark.parametrize(
        "bad",
        ["short", "x" * 65, 'abc"}, {"admin": true', "line1 line2", "<script>alert(1)</script>"],
    )
    def test_an_unusual_inbound_id_is_replaced(self, client, bad):
        response = client.get("/healthz", headers={"X-Request-ID": bad})
        assert response.headers["X-Request-ID"] != bad
        assert len(response.headers["X-Request-ID"]) == 36


class TestLogs:
    def test_each_request_logs_one_json_line(self, client, log_output):
        response = client.get("/login?q=something-private", headers={"X-Request-ID": "trace-1234"})
        entries = [e for e in json_lines(log_output()) if e["event"] == "request.finished"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["request_id"] == "trace-1234"
        assert entry["path"] == "/login"
        assert entry["status"] == response.status_code
        assert entry["method"] == "GET"
        assert entry["level"] == "info"
        assert "timestamp" in entry
        assert "something-private" not in json.dumps(entry)

    def test_passwords_never_reach_the_logs(self, client, log_output):
        client.post("/login", data={"username": "alex", "password": "my-secret-passphrase"})
        assert "my-secret-passphrase" not in log_output()

    def test_health_checks_do_not_fill_the_logs(self, client, log_output):
        client.get("/healthz")
        client.get("/readyz")
        entries = [e for e in json_lines(log_output()) if e["event"] == "request.finished"]
        assert entries == []

    def test_stdlib_loggers_come_out_as_json_too(self, client, log_output):
        client.get("/healthz")
        logging.getLogger("some.library").warning("hello from a library")
        assert any(e["event"] == "hello from a library" for e in json_lines(log_output()))


class TestHealth:
    def test_healthz_checks_nothing_but_the_process(self, client, monkeypatch):
        def fail(*_args, **_kwargs):
            raise AssertionError("healthz must not touch the database")

        monkeypatch.setattr(db.session, "execute", fail)
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json == {"status": "ok"}

    def test_readyz_when_all_is_well(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json == {"status": "ok", "database": "ok", "redis": "ok"}

    def test_readyz_is_503_without_the_database(self, client, monkeypatch):
        def fail(*_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused to db:5432"))

        monkeypatch.setattr(db.session, "execute", fail)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json["database"] == "unavailable"
        assert "5432" not in response.text, "no connection details are exposed"

    def test_readyz_is_still_ready_without_redis(self, client, monkeypatch):
        def fail(*_args, **_kwargs):
            raise redis.ConnectionError("no route to redis:6379")

        monkeypatch.setattr(redis.Redis, "ping", fail)
        response = client.get("/readyz")
        assert response.status_code == 200, "rate limits fall back to memory"
        assert response.json["redis"] == "degraded"
        assert "6379" not in response.text

    def test_health_checks_are_not_rate_limited(self, client):
        for _ in range(50):
            assert client.get("/healthz").status_code == 200

    def test_health_checks_set_no_cookie(self, client):
        assert "Set-Cookie" not in client.get("/readyz").headers
