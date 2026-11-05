"""Command-line tasks: ``flask users create-admin``."""

from __future__ import annotations

import click
from flask import Flask
from flask.cli import AppGroup

from app.services import auth
from app.services.errors import ValidationFailed

users_cli = AppGroup("users", help="Manage user accounts.")


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


def init_app(app: Flask) -> None:
    app.cli.add_command(users_cli)
