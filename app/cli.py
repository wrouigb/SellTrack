"""Command-line tasks: ``flask users create-admin``, ``flask jobs scheduler``, and so on."""

from __future__ import annotations

import os
import signal
import threading
from types import FrameType

import click
from flask import Flask
from flask.cli import AppGroup

from app.jobs.cleanup import run_cleanup
from app.jobs.scheduler import run_forever
from app.jobs.setup import database_is_ahead, ensure_smoke_account, run_migrations
from app.services import auth
from app.services.errors import ValidationFailed

users_cli = AppGroup("users", help="Manage user accounts.")
jobs_cli = AppGroup("jobs", help="Run background work.")


@users_cli.command("create-admin")
@click.option("--username", prompt=True, help="Username for the new admin.")
@click.password_option(help="Prompted for when not given, so it stays out of shell history.")
def create_admin(username: str, password: str) -> None:
    """Create an admin account."""
    try:
        user = auth.create_admin(username, password)
    except ValidationFailed as error:
        for message in error.errors.values():
            click.echo(f"Error: {message}", err=True)
        raise SystemExit(1) from None
    click.echo(f"Admin account '{user.username}' created.")


@users_cli.command("ensure-smoke-account")
def smoke_account() -> None:
    """Make the account that the check after each deploy logs in to, from SMOKE_* settings."""
    username = os.environ.get("SMOKE_USERNAME")
    password = os.environ.get("SMOKE_PASSWORD")
    if not username or not password:
        click.echo("SMOKE_USERNAME and SMOKE_PASSWORD aren't set, so there's no smoke account.")
        return
    try:
        ensure_smoke_account(username, password)
    except ValidationFailed as error:
        for message in error.errors.values():
            click.echo(f"Error: {message}", err=True)
        raise SystemExit(1) from None
    click.echo(f"Smoke account '{username}' is ready.")


@jobs_cli.command("migrate")
def migrate() -> None:
    """Bring the database up to date, unless it's already ahead of this release."""
    if database_is_ahead():
        click.echo("The database is ahead of this release, which is fine. Nothing to do.")
        return
    run_migrations()
    click.echo("The database is up to date.")


@jobs_cli.command("cleanup")
def cleanup() -> None:
    """Remove what's past keeping, right now."""
    result = run_cleanup()
    if result is None:
        click.echo("A cleanup is already running.", err=True)
        raise SystemExit(1)
    click.echo(
        f"Removed {result.sales} deleted sales, {result.sign_ins} old sign-ins "
        f"and {result.notifications} old notifications."
    )


@jobs_cli.command("scheduler")
def scheduler() -> None:
    """Run the nightly cleanup, until told to stop."""
    stop = threading.Event()

    def request_stop(_signal: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_forever(stop)


def init_app(app: Flask) -> None:
    app.cli.add_command(users_cli)
    app.cli.add_command(jobs_cli)
