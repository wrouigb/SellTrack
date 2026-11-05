"""Errors raised by services. Messages are written for the person using the app."""

from __future__ import annotations

from collections.abc import Mapping


class ServiceError(Exception):
    """Base class for an expected failure of a business rule."""


class NotFoundError(ServiceError):
    """The record doesn't exist or belongs to another account. Deliberately the same error."""


class ValidationFailed(ServiceError):  # noqa: N818 - reads better at call sites than ...Error
    """The input was wrong. ``errors`` maps a field name to what's wrong with it."""

    def __init__(self, errors: Mapping[str, str] | None = None, **field_errors: str) -> None:
        self.errors: dict[str, str] = {**(errors or {}), **field_errors}
        super().__init__("; ".join(self.errors.values()))


class ConflictError(ValidationFailed):
    """The input was fine, but clashes with what's already saved (e.g. a name that's taken)."""


class LockedError(ServiceError):
    """A record in a settled period can't have its money changed until the settle-up is undone."""


class AuthenticationError(ServiceError):
    """Wrong username or password, or wrong recovery code. Never says which."""


class AccountSuspendedError(ServiceError):
    """The right credentials, on an account an admin has suspended."""


class CannotSettleError(ServiceError):
    """The period can't be settled yet. ``issues`` says why."""

    def __init__(self, message: str, issues: tuple[str, ...] = ()) -> None:
        self.issues = issues
        super().__init__(message)
