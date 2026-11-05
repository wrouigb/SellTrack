"""Turning raw input into clean values, or a message saying what's wrong with it."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.money import InvalidMoneyError, InvalidPercentError, to_money, to_percent
from app.services.errors import ValidationFailed

#: Quantities are capped so that quantity × cost can't overflow a money column.
MAX_QUANTITY = 1_000_000
_EARLIEST_DATE = date(2000, 1, 1)
_LATEST_DATE = date(2100, 1, 1)
_WHOLE_NUMBER = re.compile(r"^\d+$", re.ASCII)


class Errors:
    """Collects a problem per field, so a form can show them all at once."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}

    def add(self, field: str, message: str) -> None:
        self.fields.setdefault(field, message)

    def raise_if_any(self) -> None:
        """Stop if anything was wrong, ending the transaction so that no row stays locked."""
        if self.fields:
            db.session.rollback()
            raise ValidationFailed(self.fields)


def checked[T](value: T | None) -> T:
    """A cleaned value, once ``Errors.raise_if_any`` has established that it isn't ``None``."""
    if value is None:
        raise AssertionError("errors.raise_if_any() should have stopped this")
    return value


def clean_money(
    errors: Errors,
    field: str,
    value: Decimal | int | str | None,
    *,
    required: bool = True,
    allow_zero: bool = True,
) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors.add(field, "Enter an amount.")
        return None
    try:
        return to_money(value, allow_zero=allow_zero)
    except InvalidMoneyError as error:
        errors.add(field, str(error))
    except TypeError:
        errors.add(field, "Enter an amount in dollars and cents, like 12.50.")
    return None


def clean_percent(errors: Errors, field: str, value: Decimal | int | str | None) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.add(field, "Enter a share between 0 and 100.")
        return None
    try:
        return to_percent(value)
    except InvalidPercentError as error:
        errors.add(field, str(error))
    except TypeError:
        errors.add(field, "Enter a share between 0 and 100, like 50 or 33.33.")
    return None


def clean_text(
    errors: Errors,
    field: str,
    value: str | None,
    *,
    max_length: int,
    required: bool = False,
    label: str = "This",
) -> str | None:
    """Strip whitespace. Optional text that's empty becomes ``None``."""
    text = (value or "").replace("\x00", "").strip()
    if not text:
        if required:
            errors.add(field, f"{label} can't be empty.")
        return None
    if len(text) > max_length:
        errors.add(field, f"Use at most {max_length} characters.")
        return None
    return text


def clean_quantity(errors: Errors, field: str, value: int | str | None) -> int | None:
    if isinstance(value, str) and _WHOLE_NUMBER.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.add(field, "Enter a whole number of 1 or more.")
        return None
    if value > MAX_QUANTITY:
        errors.add(field, f"The quantity can be at most {MAX_QUANTITY:,}.")
        return None
    return value


def clean_date(errors: Errors, field: str, value: date | str | None) -> date | None:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError:
            errors.add(field, "Enter a valid date.")
            return None
    if not isinstance(value, date):
        errors.add(field, "Enter a date.")
        return None
    if not _EARLIEST_DATE <= value < _LATEST_DATE:
        errors.add(field, "Enter a date between 2000 and 2099.")
        return None
    return value


def violated_constraint(error: IntegrityError) -> str:
    """The name of the database constraint that an insert or update broke."""
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None) or ""
