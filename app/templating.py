"""Template filters. Dates and money are always shown in en-AU style."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import Flask

from app.money import format_aud


def format_date(value: date | datetime | None) -> str:
    return value.strftime("%d/%m/%Y") if value is not None else ""


def money(value: Decimal | int | None) -> str:
    return format_aud(value) if value is not None else ""


def init_app(app: Flask) -> None:
    app.add_template_filter(money, "money")
    app.add_template_filter(format_date, "date")
