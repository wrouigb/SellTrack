"""The check that decides whether a deploy stays, against a real running app."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import select
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db
from app.jobs.setup import ensure_smoke_account
from app.models import Sale
from tests.conftest import make_settings
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).parents[2] / "deploy/smoke.py"
USERNAME = "smoke-check"


@pytest.fixture
def server(clean_state) -> Iterator[tuple[str, object]]:
    application = create_app(make_settings(), overrides={"WTF_CSRF_ENABLED": True})
    running = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{running.server_port}", application
    running.shutdown()
    thread.join()


def smoke(url: str, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("SMOKE_", "BACKUP_", "APP_"))}
    return subprocess.run(  # noqa: S603 - our own script, in a test
        [sys.executable, str(SCRIPT), "--base-url", url, "--retries", "0", *extra],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**clean, **env},
    )


def account(application) -> None:
    with application.app_context():
        ensure_smoke_account(USERNAME, PASSWORD)
        db.session.remove()


class TestAgainstTheApp:
    def test_all_good(self, server):
        url, application = server
        account(application)
        result = smoke(url, "--no-cache-check", SMOKE_USERNAME=USERNAME, SMOKE_PASSWORD=PASSWORD)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "logged in, added a sale and deleted it" in result.stdout

    def test_it_cleans_up_after_itself(self, server):
        url, application = server
        account(application)
        env = {"SMOKE_USERNAME": USERNAME, "SMOKE_PASSWORD": PASSWORD}
        assert smoke(url, "--no-cache-check", **env).returncode == 0
        assert smoke(url, "--no-cache-check", **env).returncode == 0
        with application.app_context():
            rows = db.session.scalars(select(Sale)).all()
            assert len(rows) == 2
            assert all(sale.deleted_at is not None for sale in rows)
            db.session.remove()

    def test_a_wrong_password_fails(self, server):
        url, application = server
        account(application)
        result = smoke(url, "--no-cache-check", SMOKE_USERNAME=USERNAME, SMOKE_PASSWORD="wrong")
        assert result.returncode == 1
        assert "logging in returned 401" in result.stderr

    def test_an_account_with_nothing_to_sell_fails(self, server):
        url, application = server
        with application.app_context():
            from app.services import auth

            auth.register(USERNAME, PASSWORD)
            db.session.remove()
        result = smoke(url, "--no-cache-check", SMOKE_USERNAME=USERNAME, SMOKE_PASSWORD=PASSWORD)
        assert result.returncode == 1
        assert "nothing to choose" in result.stderr or "no product_id" in result.stderr

    def test_without_an_account_it_says_login_wasnt_checked(self, server):
        url, _application = server
        result = smoke(url, "--no-cache-check")
        assert result.returncode == 0
        assert "login wasn't checked" in result.stdout

    def test_notices_when_assets_arent_cached(self, server):
        """They aren't when served by Flask, so this is what a missing Nginx would look like."""
        url, _application = server
        result = smoke(url)
        assert result.returncode == 1
        assert "wrong Cache-Control" in result.stderr

    def test_notices_a_missing_hsts_header(self, server):
        url, _application = server
        result = smoke(url, "--no-cache-check", APP_ENV="production")
        assert result.returncode == 1
        assert "no HSTS header" in result.stderr


class TestBackupWarnings:
    def test_a_late_backup_is_only_a_warning(self, server, tmp_path):
        """A new release didn't cause it, so going back to the old one wouldn't help."""
        url, _application = server
        backup = tmp_path / "selltrack-old.dump"
        backup.write_text("x")
        stamp = (datetime.now(UTC) - timedelta(hours=30)).timestamp()
        os.utime(backup, (stamp, stamp))
        result = smoke(url, "--no-cache-check", BACKUP_DIR=str(tmp_path))
        assert result.returncode == 0
        assert "the last backup is" in result.stdout

    def test_no_backups_yet_is_only_a_warning(self, server, tmp_path):
        url, _application = server
        (tmp_path / ".in-progress-1").write_text("x")
        result = smoke(url, "--no-cache-check", BACKUP_DIR=str(tmp_path))
        assert result.returncode == 0
        assert "no backups yet" in result.stdout

    def test_a_recent_backup_says_nothing(self, server, tmp_path):
        url, _application = server
        (tmp_path / "selltrack-new.dump").write_text("x")
        result = smoke(url, "--no-cache-check", BACKUP_DIR=str(tmp_path))
        assert result.returncode == 0
        assert "backup" not in result.stdout


class _Broken(BaseHTTPRequestHandler):
    responses_by_path: ClassVar[dict[str, tuple[int, str]]] = {}

    def do_GET(self) -> None:
        status, body = self.responses_by_path.get(self.path, (404, "nope"))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def broken() -> Iterator[type[_Broken]]:
    _Broken.responses_by_path = {}
    running = HTTPServer(("127.0.0.1", 0), _Broken)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    _Broken.url = f"http://127.0.0.1:{running.server_port}"
    yield _Broken
    running.shutdown()
    thread.join()


class TestAgainstSomethingBroken:
    def test_nothing_listening(self):
        result = smoke("http://127.0.0.1:1")
        assert result.returncode == 1
        assert "never became healthy" in result.stderr

    def test_not_ready(self, broken):
        broken.responses_by_path = {"/healthz": (200, "ok"), "/readyz": (503, "database down")}
        result = smoke(broken.url)
        assert result.returncode == 1
        assert "database down" in result.stderr

    def test_no_security_headers(self, broken):
        broken.responses_by_path = {
            "/healthz": (200, "ok"),
            "/readyz": (200, "ok"),
            "/": (200, "<html></html>"),
        }
        result = smoke(broken.url)
        assert result.returncode == 1
        assert "no Content-Security-Policy header" in result.stderr

    def test_it_waits_for_a_while_first(self):
        result = subprocess.run(  # noqa: S603 - our own script, in a test
            [
                sys.executable,
                str(SCRIPT),
                "--base-url",
                "http://127.0.0.1:1",
                "--retries",
                "2",
                "--wait-seconds",
                "0.01",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
