"""WTForms form classes. Business rules live in services; forms handle fields and CSRF."""

from __future__ import annotations

from typing import Any

from flask import flash

from app.services.errors import ValidationFailed

_MAX_BIGINT = 2**63 - 1


def to_id(value: Any) -> int | None:
    """A submitted ID as a number, or ``None``. Whether it's theirs is for the service to check."""
    text = str(value if value is not None else "").strip()
    if not text.isascii() or not text.isdecimal():
        return None
    number = int(text)
    return number if number <= _MAX_BIGINT else None


def show_errors(form: Any, error: ValidationFailed) -> None:
    """Put a service's messages on the matching fields, or show them at the top if there's none."""
    for name, message in error.errors.items():
        field = getattr(form, name, None)
        if field is not None and hasattr(field, "errors"):
            field.errors = [*field.errors, message]
        else:
            flash(message, "error")
