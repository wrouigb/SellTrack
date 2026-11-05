"""Template filters and helpers. Dates and money are always shown in en-AU style."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, g, request, session
from flask_login import current_user

from app.models import DEFAULT_TIMEZONE, Theme
from app.money import format_aud


def format_date(value: date | datetime | None) -> str:
    return value.strftime("%d/%m/%Y") if value is not None else ""


def money(value: Decimal | int | None) -> str:
    return format_aud(value) if value is not None else ""


def signed_money(value: Decimal) -> str:
    """Money with its sign always shown, for changes and balances: ``+$3.00`` or ``−$3.00``."""
    return f"+{format_aud(value)}" if value > 0 else format_aud(value)


def snapshot_money(value: str) -> str:
    """An amount from a settlement's saved snapshot, where they're stored as text."""
    return format_aud(Decimal(value))


def percent(value: Decimal | None) -> str:
    return "" if value is None else f"{value}%"


def input_money(value: Decimal | None) -> str:
    """An amount as it should appear inside a form field."""
    return "" if value is None else str(value)


def local_datetime(value: datetime | None) -> str:
    """A moment in time, in the logged-in person's time zone."""
    if value is None:
        return ""
    name = current_user.timezone if current_user.is_authenticated else DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    local = value.astimezone(zone)
    return f"{local:%d/%m/%Y} {local:%-I:%M %p}".replace("AM", "am").replace("PM", "pm")


def current_theme() -> str:
    """The theme for this page. Error pages skip the lookup, as the database may be what failed."""
    if "_user_id" not in session or g.get("rendering_error"):
        return Theme.SYSTEM.value
    if current_user.is_authenticated:
        return str(current_user.theme.value)
    return Theme.SYSTEM.value


def url_with(**changes: Any) -> str:
    """This page's address with some query values changed. ``None`` removes a value."""
    values = request.args.to_dict()
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    query = urlencode(values)
    return f"{request.path}?{query}" if query else request.path


def init_app(app: Flask) -> None:
    app.add_template_filter(money, "money")
    app.add_template_filter(signed_money, "signed_money")
    app.add_template_filter(snapshot_money, "snapshot_money")
    app.add_template_filter(percent, "percent")
    app.add_template_filter(input_money, "input_money")
    app.add_template_filter(format_date, "date")
    app.add_template_filter(local_datetime, "local_datetime")
    app.jinja_env.globals["current_theme"] = current_theme
    app.jinja_env.globals["url_with"] = url_with
