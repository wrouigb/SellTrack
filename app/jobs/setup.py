"""One-off steps that a deploy runs: migrations, and the account its smoke check uses."""

from __future__ import annotations

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask import current_app
from flask_migrate import upgrade
from sqlalchemy import select

from app.extensions import db
from app.models import User
from app.security import passwords
from app.services import auth, products, split


def database_is_ahead() -> bool:
    """True if the database has migrations this release doesn't know about.

    That's what rolling back looks like. Migrations always leave the schema working for the release
    before, so an older release can run as it is, but Alembic would refuse to run at all.
    """
    config = current_app.extensions["migrate"].migrate.get_config()
    known = {revision.revision for revision in ScriptDirectory.from_config(config).walk_revisions()}
    with db.engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_heads()
    return any(revision not in known for revision in current)


def run_migrations() -> None:
    upgrade()


def ensure_smoke_account(username: str, password: str) -> None:
    """An ordinary account with what it takes to add a sale. Safe to run after every deploy."""
    user = db.session.scalar(select(User).where(User.username == username))
    if user is None:
        user = auth.register(username, password).user
        auth.complete_onboarding(user)
    elif not passwords.verify_password(user.password_hash, password):
        auth.set_password_from_command_line(user, password)
    if not products.list_products(user.id):
        products.create_product(user.id, name="Smoke check item", cost="0.50", sell_price="1.00")
    if not split.list_people(user.id):
        split.add_person(user.id, name="Smoke check", share_pct="100")
