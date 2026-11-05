"""The database roles, on a real server: what each can do, and what it can't.

Creating roles takes a superuser, so these need ``TEST_ADMIN_DATABASE_URL``. ``make test`` sets it,
so CI always runs them. They use a database of their own, and the backup and restore at the end
follow ``docs/runbooks/restore.md``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa
from flask_migrate import upgrade
from sqlalchemy.exc import ProgrammingError

from app import create_app
from app.config import normalise_database_url
from app.extensions import db
from app.jobs.cleanup import run_cleanup
from app.models import AuditLog, User
from app.services import auth, export, products, sales, split, support
from tests.conftest import make_settings
from tests.factories import PASSWORD

ROOT = Path(__file__).parents[2]
ROLES_SQL = ROOT / "deploy/db-roles.sql"
BACKUP_SH = ROOT / "deploy/backup.sh"
DATABASE = "selltrack_roles_test"
RESTORED = "selltrack_restore_test"
DAY = "2026-11-05"

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ADMIN_URL, reason="TEST_ADMIN_DATABASE_URL isn't set"),
]


def url_for(user: str, password: str, database: str) -> str:
    parts = urlsplit(ADMIN_URL)
    return urlunsplit(
        (
            parts.scheme,
            f"{user}:{password}@{parts.hostname}:{parts.port or 5432}",
            f"/{database}",
            "",
            "",
        )
    )


def admin_url(database: str) -> str:
    parts = urlsplit(ADMIN_URL)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def engine_for(url: str) -> sa.Engine:
    return sa.create_engine(normalise_database_url(url), isolation_level="AUTOCOMMIT")


def recreate(database: str) -> None:
    engine = engine_for(ADMIN_URL)
    with engine.connect() as connection:
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
    engine.dispose()


def drop(database: str) -> None:
    engine = engine_for(ADMIN_URL)
    with engine.connect() as connection:
        connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    engine.dispose()


@dataclass
class Passwords:
    migrator: str
    app: str
    backup: str

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "DB_MIGRATOR_PASSWORD": self.migrator,
            "DB_APP_PASSWORD": self.app,
            "DB_BACKUP_PASSWORD": self.backup,
        }


def apply_roles(passwords: Passwords, database: str = DATABASE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script, in a test
        ["psql", "--quiet", f"--dbname={admin_url(database)}", f"--file={ROLES_SQL}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=passwords.environment(),
    )


@pytest.fixture(scope="module")
def passwords() -> Iterator[Passwords]:
    if not shutil.which("psql"):
        pytest.fail("psql isn't installed, and these need it.", pytrace=False)
    recreate(DATABASE)
    chosen = Passwords(secrets.token_hex(16), secrets.token_hex(16), secrets.token_hex(16))
    result = apply_roles(chosen)
    assert result.returncode == 0, result.stderr

    application = create_app(
        make_settings(database_url=url_for("selltrack_migrator", chosen.migrator, DATABASE))
    )
    with application.app_context():
        upgrade()
        db.engine.dispose()

    result = apply_roles(chosen)
    assert result.returncode == 0, result.stderr
    yield chosen
    drop(DATABASE)
    drop(RESTORED)
    create_app(make_settings())  # points the shared rate limiter back at the usual Redis


@pytest.fixture
def app_engine(passwords) -> Iterator[sa.Engine]:
    engine = engine_for(url_for("selltrack_app", passwords.app, DATABASE))
    yield engine
    engine.dispose()


@pytest.fixture
def migrator_engine(passwords) -> Iterator[sa.Engine]:
    engine = engine_for(url_for("selltrack_migrator", passwords.migrator, DATABASE))
    yield engine
    engine.dispose()


@pytest.fixture
def backup_engine(passwords) -> Iterator[sa.Engine]:
    engine = engine_for(url_for("selltrack_backup", passwords.backup, DATABASE))
    yield engine
    engine.dispose()


def refused(engine: sa.Engine, statement: str) -> None:
    with engine.connect() as connection, pytest.raises(ProgrammingError) as caught:
        connection.execute(sa.text(statement))
    assert "permission denied" in str(caught.value) or "must be owner" in str(caught.value)


class TestTheApp:
    def test_everything_the_app_does_works_with_its_role(self, passwords):
        """The app, on the role it gets in production, doing a bit of everything."""
        application = create_app(
            make_settings(database_url=url_for("selltrack_app", passwords.app, DATABASE))
        )
        with application.app_context():
            account = auth.register("role-check", PASSWORD)
            user_id = account.user.id
            auth.complete_onboarding(account.user)
            product = products.create_product(user_id, name="Stamp", cost="2", sell_price="5")
            products.update_product(user_id, product.id, name="Stamp 2", cost="3", sell_price="5")
            alex = split.add_person(user_id, name="Alex", share_pct="50")
            sam = split.add_person(user_id, name="Sam", share_pct="50")
            sale = sales.create_sale(
                user_id,
                product_id=product.id,
                sale_date=DAY,
                quantity=1,
                price_paid="10",
                received_by_partner_id=sam.id,
            )
            split.add_cost(
                user_id,
                description="Stall",
                category="fees",
                amount="4",
                cost_date=DAY,
                paid_by_partner_id=alex.id,
            )
            split.record_payment(
                user_id, from_partner_id=sam.id, to_partner_id=alex.id, amount="1", paid_on=DAY
            )
            split.settle_up(user_id, actor_id=user_id)
            split.undo_last_settlement(user_id, actor_id=user_id)
            sales.delete_sale(user_id, sale.id)
            support.send_message(user_id, subject="Hello", body="Hi")
            assert run_cleanup() is not None
            archive, size = export.account_zip(user_id)
            archive.close()
            assert size > 0
            db.session.remove()
            db.engine.dispose()

    def test_deleting_an_account_still_unlinks_its_audit_entries(self, passwords):
        """The app can't update the audit log, but the foreign key's own action still happens."""
        application = create_app(
            make_settings(database_url=url_for("selltrack_app", passwords.app, DATABASE))
        )
        with application.app_context():
            user = auth.register("leaving", PASSWORD).user
            user_id = user.id
            auth.delete_account(user, PASSWORD)
            entries = db.session.scalars(
                sa.select(AuditLog).where(
                    AuditLog.target_id == user_id, AuditLog.action == "auth.registered"
                )
            ).all()
            assert [entry.actor_id for entry in entries] == [None]
            db.session.remove()
            db.engine.dispose()

    def test_it_can_add_to_the_audit_log(self, app_engine):
        with app_engine.connect() as connection:
            connection.execute(sa.text("INSERT INTO audit_logs (action) VALUES ('test.entry')"))

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE audit_logs SET action = 'changed'",
            "DELETE FROM audit_logs",
            "TRUNCATE audit_logs",
            "UPDATE alembic_version SET version_num = 'x'",
            "DELETE FROM alembic_version",
            "CREATE TABLE sneaky (id int)",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN sneaky int",
            "ALTER TABLE audit_logs DISABLE TRIGGER ALL",
            "CREATE ROLE sneaky",
        ],
    )
    def test_what_it_cant_do(self, app_engine, statement):
        refused(app_engine, statement)


class TestTheMigrator:
    def test_isnt_a_superuser(self, migrator_engine):
        with migrator_engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
        assert tuple(row) == (False, False, False)

    def test_can_change_the_schema(self, migrator_engine):
        with migrator_engine.connect() as connection:
            connection.execute(sa.text("CREATE TABLE temporary_check (id int)"))
            connection.execute(sa.text("DROP TABLE temporary_check"))

    def test_new_tables_are_usable_by_the_app(self, migrator_engine, app_engine, backup_engine):
        with migrator_engine.connect() as connection:
            connection.execute(sa.text("CREATE TABLE later_table (id bigserial, name text)"))
        try:
            with app_engine.connect() as connection:
                connection.execute(sa.text("INSERT INTO later_table (name) VALUES ('x')"))
            with backup_engine.connect() as connection:
                connection.execute(sa.text("SELECT * FROM later_table"))
        finally:
            with migrator_engine.connect() as connection:
                connection.execute(sa.text("DROP TABLE later_table"))


class TestTheBackupRole:
    def test_reads_everything(self, backup_engine):
        with backup_engine.connect() as connection:
            for table in db.metadata.sorted_tables:
                connection.execute(sa.text(f'SELECT count(*) FROM "{table.name}"'))  # noqa: S608

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO audit_logs (action) VALUES ('x')",
            "DELETE FROM users",
            "UPDATE users SET username = 'x'",
            "CREATE TABLE sneaky (id int)",
        ],
    )
    def test_changes_nothing(self, backup_engine, statement):
        refused(backup_engine, statement)


class TestTheScript:
    def test_can_be_run_again(self, passwords):
        result = apply_roles(passwords)
        assert result.returncode == 0, result.stderr

    def test_is_how_passwords_change(self, passwords):
        changed = Passwords(passwords.migrator, secrets.token_hex(16), passwords.backup)
        assert apply_roles(changed).returncode == 0
        engine = engine_for(url_for("selltrack_app", changed.app, DATABASE))
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
        engine.dispose()
        assert apply_roles(passwords).returncode == 0

    def test_refuses_to_run_without_every_password(self, passwords):
        environment = passwords.environment()
        del environment["DB_APP_PASSWORD"]
        result = subprocess.run(  # noqa: S603 - our own script, in a test
            ["psql", "--quiet", f"--dbname={admin_url(DATABASE)}", f"--file={ROLES_SQL}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode != 0

    def test_doesnt_undo_the_audit_rule(self, passwords, app_engine):
        assert apply_roles(passwords).returncode == 0
        refused(app_engine, "DELETE FROM audit_logs")


class TestBackupAndRestore:
    """The whole of docs/runbooks/restore.md, on real files."""

    def test_a_backup_can_be_restored(self, passwords, tmp_path):
        application = create_app(
            make_settings(database_url=url_for("selltrack_app", passwords.app, DATABASE))
        )
        with application.app_context():
            user_id = auth.register("restore-check", PASSWORD).user.id
            product = products.create_product(
                user_id, name="José's stamp", cost="2", sell_price="5"
            )
            person = split.add_person(user_id, name="Alex", share_pct="100")
            sales.create_sale(
                user_id,
                product_id=product.id,
                sale_date=DAY,
                quantity=1,
                price_paid="123.45",
                received_by_partner_id=person.id,
            )
            count = db.session.scalar(sa.select(sa.func.count()).select_from(User))
            db.session.remove()
            db.engine.dispose()

        # The backup, as the read-only role, with the real script and the real pg_dump.
        parts = urlsplit(ADMIN_URL)
        result = subprocess.run(  # noqa: S603 - our own script, in a test
            ["sh", str(BACKUP_SH), "--once"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "BACKUP_DIR": str(tmp_path),
                "PGHOST": parts.hostname,
                "PGPORT": str(parts.port or 5432),
                "PGUSER": "selltrack_backup",
                "PGPASSWORD": passwords.backup,
                "PGDATABASE": DATABASE,
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        [dump] = list(tmp_path.glob("selltrack-*.dump"))

        # An empty database, as after losing everything.
        recreate(RESTORED)
        with dump.open("rb") as archive:
            result = subprocess.run(  # noqa: S603 - our own command, in a test
                ["pg_restore", f"--dbname={admin_url(RESTORED)}", "--exit-on-error"],  # noqa: S607
                stdin=archive,
                capture_output=True,
                text=True,
                check=False,
            )
        assert result.returncode == 0, result.stderr
        result = apply_roles(passwords, RESTORED)
        assert result.returncode == 0, result.stderr

        # The app works on it, as its usual role, with everything there.
        restored = create_app(
            make_settings(database_url=url_for("selltrack_app", passwords.app, RESTORED))
        )
        with restored.app_context():
            assert db.session.scalar(sa.select(sa.func.count()).select_from(User)) == count
            user = auth.authenticate("restore-check", PASSWORD)
            assert sales.list_sales(user.id).totals.money_in == Decimal("123.45")
            assert [row.product.name for row in products.list_products(user.id)] == ["José's stamp"]
            db.session.remove()
            db.engine.dispose()

        # And the rules still hold.
        engine = engine_for(url_for("selltrack_app", passwords.app, RESTORED))
        refused(engine, "DELETE FROM audit_logs")
        refused(engine, "CREATE TABLE sneaky (id int)")
        engine.dispose()

        # So do the migrations, which is what the next deploy runs.
        migrator = create_app(
            make_settings(database_url=url_for("selltrack_migrator", passwords.migrator, RESTORED))
        )
        with migrator.app_context():
            upgrade()
            db.engine.dispose()
