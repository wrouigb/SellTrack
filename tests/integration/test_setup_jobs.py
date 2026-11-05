"""The one-off steps a deploy runs."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.extensions import db
from app.jobs.setup import database_is_ahead, ensure_smoke_account
from app.models import Partner, Product, User
from app.services import auth, system
from app.services.errors import AuthenticationError
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

NEW_PASSWORD = "another-long-passphrase"


class TestMigrate:
    @pytest.fixture
    def from_the_future(self, ctx):
        """A database migrated by a later release than this one, as after rolling back."""
        with db.engine.begin() as connection:
            current = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = '9999_from_the_future'")
            )
        yield
        with db.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num = :version"), {"version": current}
            )

    def test_up_to_date_is_fine(self, app, ctx):
        assert not database_is_ahead()
        result = app.test_cli_runner().invoke(args=["jobs", "migrate"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_a_database_ahead_of_this_release_is_left_alone(self, app, from_the_future):
        assert database_is_ahead()
        result = app.test_cli_runner().invoke(args=["jobs", "migrate"])
        assert result.exit_code == 0, result.output
        assert "ahead of this release" in result.output
        with db.engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
        assert version == "9999_from_the_future"


class TestSmokeAccount:
    def test_makes_an_account_ready_to_sell(self, ctx):
        ensure_smoke_account("smoke-check", PASSWORD)
        user = db.session.scalar(select(User).where(User.username == "smoke-check"))
        assert user.onboarded_at is not None
        assert not user.is_admin
        assert db.session.scalar(select(Product).where(Product.user_id == user.id))
        assert db.session.scalar(select(Partner).where(Partner.user_id == user.id))

    def test_running_it_again_changes_nothing(self, ctx):
        ensure_smoke_account("smoke-check", PASSWORD)
        ensure_smoke_account("smoke-check", PASSWORD)
        user_id = db.session.scalar(select(User.id))
        rows = db.session.scalars(select(Product).where(Product.user_id == user_id)).all()
        assert len(rows) == 1

    def test_a_new_password_takes_over(self, ctx):
        ensure_smoke_account("smoke-check", PASSWORD)
        ensure_smoke_account("smoke-check", NEW_PASSWORD)
        assert auth.authenticate("smoke-check", NEW_PASSWORD)
        with pytest.raises(AuthenticationError):
            auth.authenticate("smoke-check", PASSWORD)

    def test_the_command(self, app, ctx, monkeypatch):
        monkeypatch.setenv("SMOKE_USERNAME", "smoke-check")
        monkeypatch.setenv("SMOKE_PASSWORD", PASSWORD)
        result = app.test_cli_runner().invoke(args=["users", "ensure-smoke-account"])
        assert result.exit_code == 0, result.output
        assert "is ready" in result.output

    def test_the_command_without_settings(self, app, ctx, monkeypatch):
        monkeypatch.delenv("SMOKE_USERNAME", raising=False)
        monkeypatch.delenv("SMOKE_PASSWORD", raising=False)
        result = app.test_cli_runner().invoke(args=["users", "ensure-smoke-account"])
        assert result.exit_code == 0
        assert "no smoke account" in result.output
        assert db.session.scalar(select(User.id)) is None

    def test_the_command_with_a_weak_password(self, app, ctx, monkeypatch):
        monkeypatch.setenv("SMOKE_USERNAME", "smoke-check")
        monkeypatch.setenv("SMOKE_PASSWORD", "short")
        result = app.test_cli_runner().invoke(args=["users", "ensure-smoke-account"])
        assert result.exit_code == 1
        assert "Error:" in result.output


def test_an_unfinished_backup_doesnt_count(tmp_path):
    (tmp_path / ".in-progress-20261105").write_text("partial")
    backup = system.last_backup(tmp_path)
    assert backup.last_at is None
    assert backup.is_late
