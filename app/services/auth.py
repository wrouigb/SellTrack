"""Accounts: sign-up, login, recovery codes, passwords and sign-in history.

There is no email (plan decision 3), so a forgotten password is handled by
one-time recovery codes shown once at sign-up, or by an admin reset.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import available_timezones

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import RecoveryCode, SignIn, Theme, User, UserRole, UserStatus
from app.security import passwords
from app.services import audit
from app.services.errors import (
    AccountSuspendedError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationFailed,
)
from app.services.notifications import Kind, notify

RECOVERY_CODE_COUNT = 10
#: Warn the user once this few recovery codes are left.
RECOVERY_CODES_LOW = 2
RECENT_SIGN_INS = 20
USERNAME_MIN = 3
USERNAME_MAX = 30

_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,30}$", re.ASCII)
#: Crockford base32: no I, L, O or U, so a code copied by hand is hard to get wrong.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LENGTH = 16  # 80 bits of randomness
_CODE_GROUP = 4
_CODE_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1"})
_TEMP_PASSWORD_BYTES = 12
_TIMEZONE_REGIONS = {
    "Africa",
    "America",
    "Antarctica",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
}


@dataclass(frozen=True, slots=True)
class NewAccount:
    """A new account and its recovery codes. This is the only time the codes are available."""

    user: User
    recovery_codes: tuple[str, ...]


def username_problem(username: str) -> str | None:
    if not USERNAME_MIN <= len(username) <= USERNAME_MAX:
        return "Username must be 3 to 30 characters."
    if not _USERNAME.fullmatch(username):
        return "Username can only use letters, numbers, . _ and -"
    return None


def register(username: str, password: str) -> NewAccount:
    """Create a normal account. Raises ``ValidationFailed`` or ``ConflictError``."""
    return _create_account(username, password, role=UserRole.USER, action="auth.registered")


def create_admin(username: str, password: str) -> User:
    """Create an admin account. Only reachable from the command line."""
    account = _create_account(username, password, role=UserRole.ADMIN, action="auth.admin_created")
    return account.user


def _create_account(username: str, password: str, *, role: UserRole, action: str) -> NewAccount:
    username = (username or "").strip()
    errors: dict[str, str] = {}
    if problem := username_problem(username):
        errors["username"] = problem
    if problem := passwords.password_problem(password or "", username):
        errors["password"] = problem
    if errors:
        raise ValidationFailed(errors)

    user = User(username=username, password_hash=passwords.hash_password(password), role=role)
    db.session.add(user)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise ConflictError(username="That username is already taken.") from None
    codes = _issue_recovery_codes(user.id)
    audit.record(action, actor_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()
    return NewAccount(user=user, recovery_codes=codes)


def authenticate(username: str, password: str) -> User:
    """Check a login and record it. Raises ``AuthenticationError`` or ``AccountSuspendedError``.

    An unknown username takes as long as a wrong password and produces the same error, so
    logins can't be used to find out which usernames exist.
    """
    username = (username or "").strip()
    user = _find_by_username(username)
    if user is None:
        passwords.burn_verification_time(password or "")
        _record_failed_login(None)
        raise AuthenticationError("Wrong username or password.")
    if not passwords.verify_password(user.password_hash, password or ""):
        _record_failed_login(user.id)
        raise AuthenticationError("Wrong username or password.")

    # Only someone with the right password learns the account is suspended.
    if user.status is UserStatus.SUSPENDED:
        raise AccountSuspendedError("This account is suspended.")

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    _record_sign_in(user)
    db.session.commit()
    return user


def _record_failed_login(user_id: int | None) -> None:
    # The attempted username isn't stored: people sometimes type a password into that box.
    audit.record("auth.login_failed", target_type="user", target_id=user_id)
    db.session.commit()


def _record_sign_in(user: User) -> None:
    ip = audit.client_ip()
    agent = audit.client_user_agent()
    has_history = db.session.scalar(select(SignIn.id).where(SignIn.user_id == user.id).limit(1))
    seen_before = db.session.scalar(
        select(SignIn.id)
        .where(
            SignIn.user_id == user.id,
            SignIn.ip.is_not_distinct_from(ip),
            SignIn.user_agent.is_not_distinct_from(agent),
        )
        .limit(1)
    )
    # The first ever sign-in isn't news. Later ones from somewhere new matter more than usual,
    # because logins may be shared between partners.
    if has_history and not seen_before:
        notify(
            user.id,
            Kind.NEW_SIGN_IN,
            "New sign-in to your account",
            f"From {ip or 'an unknown address'} on {agent or 'an unknown device'}. "
            "If this wasn't you, change your password and log out everywhere.",
            url="/app/settings/security",
        )
    db.session.add(SignIn(user_id=user.id, ip=ip, user_agent=agent))
    user.last_login_at = datetime.now(UTC)
    audit.record("auth.login", actor_id=user.id, target_type="user", target_id=user.id)


def record_logout(user: User) -> None:
    audit.record("auth.logout", actor_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()


def change_password(user: User, current_password: str, new_password: str) -> None:
    """Change a password after checking the current one. Logs out every other device."""
    if not passwords.verify_password(user.password_hash, current_password or ""):
        raise ValidationFailed(current_password="That isn't your current password.")
    if passwords.verify_password(user.password_hash, new_password or ""):
        raise ValidationFailed(new_password="Choose a different password from your current one.")
    _set_password(user, new_password)
    audit.record("auth.password_changed", actor_id=user.id, target_type="user", target_id=user.id)
    notify(
        user.id,
        Kind.PASSWORD_CHANGED,
        "Your password was changed",
        "All other devices have been logged out.",
        url="/app/settings/security",
    )
    db.session.commit()


def set_password_from_command_line(user: User, new_password: str) -> None:
    """For someone with access to the server, who is not asked for the old one. Audit-logged."""
    _set_password(user, new_password)
    audit.record("auth.password_changed", target_type="user", target_id=user.id)
    db.session.commit()


def _set_password(user: User, new_password: str) -> None:
    if problem := passwords.password_problem(new_password or "", user.username):
        raise ValidationFailed(new_password=problem)
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    user.session_version += 1


def logout_everywhere(user: User) -> None:
    """End every session for this account, including the current one."""
    user.session_version += 1
    audit.record("auth.logout_everywhere", actor_id=user.id, target_type="user", target_id=user.id)
    db.session.commit()


def regenerate_recovery_codes(user: User, password: str) -> tuple[str, ...]:
    """Replace all recovery codes after checking the password. The old ones stop working."""
    if not passwords.verify_password(user.password_hash, password or ""):
        raise ValidationFailed(password="That isn't your current password.")
    db.session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = _issue_recovery_codes(user.id)
    audit.record(
        "auth.recovery_codes_regenerated", actor_id=user.id, target_type="user", target_id=user.id
    )
    notify(
        user.id,
        Kind.RECOVERY_CODES_REGENERATED,
        "New recovery codes created",
        "Your old recovery codes no longer work.",
        url="/app/settings/security",
    )
    db.session.commit()
    return codes


def remaining_recovery_codes(user_id: int) -> int:
    return (
        db.session.scalar(
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
        )
        or 0
    )


def recover_account(username: str, code: str, new_password: str) -> User:
    """Set a new password using a recovery code, and log out every device.

    The code is used up in the same transaction as the password change, so a code can't be
    spent twice and a failure leaves it unused.
    """
    username = (username or "").strip()
    if problem := passwords.password_problem(new_password or "", username):
        raise ValidationFailed(new_password=problem)

    user = _find_by_username(username, for_update=True)
    # Unknown username and wrong code go through the same steps and give the same error.
    code_row = _find_unused_code(user.id if user else 0, code)
    if user is None or code_row is None:
        audit.record(
            "auth.recovery_failed", target_type="user", target_id=user.id if user else None
        )
        db.session.commit()
        raise AuthenticationError("Wrong username or recovery code.")
    if user.status is UserStatus.SUSPENDED:
        db.session.rollback()
        raise AccountSuspendedError("This account is suspended.")

    code_row.used_at = datetime.now(UTC)
    _set_password(user, new_password)
    audit.record("auth.recovery_code_used", actor_id=user.id, target_type="user", target_id=user.id)
    notify(
        user.id,
        Kind.RECOVERY_CODE_USED,
        "A recovery code was used",
        "Your password was reset and all devices were logged out.",
        url="/app/settings/security",
    )
    db.session.flush()
    left = remaining_recovery_codes(user.id)
    if left <= RECOVERY_CODES_LOW:
        notify(
            user.id,
            Kind.RECOVERY_CODES_LOW,
            f"{left} recovery code{'' if left == 1 else 's'} left",
            "Create new recovery codes so you can't get locked out.",
            url="/app/settings/security",
            unless_unread=True,
        )
    db.session.commit()
    return user


def recent_sign_ins(user_id: int, limit: int = RECENT_SIGN_INS) -> Sequence[SignIn]:
    return db.session.scalars(
        select(SignIn)
        .where(SignIn.user_id == user_id)
        .order_by(SignIn.created_at.desc(), SignIn.id.desc())
        .limit(limit)
    ).all()


def delete_account(user: User, password: str) -> None:
    """Permanently delete the account and everything it owns, after checking the password."""
    if not passwords.verify_password(user.password_hash, password or ""):
        raise ValidationFailed(password="That isn't your current password.")
    audit.record("auth.account_deleted", target_type="user", target_id=user.id)
    db.session.delete(user)
    db.session.commit()


def timezone_names() -> list[str]:
    """Time zones people would choose from, Australia's first."""
    zones = (
        zone
        for zone in available_timezones()
        if "/" in zone and zone.split("/", maxsplit=1)[0] in _TIMEZONE_REGIONS
    )
    return sorted(zones, key=lambda zone: (not zone.startswith("Australia/"), zone))


def update_preferences(
    user: User, *, timezone: str | None = None, theme: str | None = None
) -> None:
    """Change the time zone used for "today", and how the app looks."""
    errors: dict[str, str] = {}
    if timezone is not None and timezone not in available_timezones():
        errors["timezone"] = "Choose a time zone from the list."
    new_theme = None
    if theme is not None:
        try:
            new_theme = Theme(theme)
        except ValueError:
            errors["theme"] = "Choose an appearance from the list."
    if errors:
        db.session.rollback()
        raise ValidationFailed(errors)
    if timezone is not None:
        user.timezone = timezone
    if new_theme is not None:
        user.theme = new_theme
    db.session.commit()


def complete_onboarding(user: User) -> None:
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(UTC)
    db.session.commit()


def admin_reset_password(actor: User, user_id: int) -> str:
    """Give a user a temporary password, which they must change at next login. Returns it."""
    if user_id == actor.id:
        raise ValidationFailed(password="Change your own password under Settings.")
    user = _get_user(user_id)
    temporary = secrets.token_urlsafe(_TEMP_PASSWORD_BYTES)
    user.password_hash = passwords.hash_password(temporary)
    user.must_change_password = True
    user.session_version += 1
    audit.record("admin.password_reset", actor_id=actor.id, target_type="user", target_id=user.id)
    notify(
        user.id,
        Kind.PASSWORD_RESET_BY_ADMIN,
        "An admin reset your password",
        "Choose a new password to continue.",
        url="/app/settings/security",
    )
    db.session.commit()
    return temporary


def set_user_status(actor: User, user_id: int, status: UserStatus) -> None:
    """Suspend or reactivate an account. Suspending takes effect on the user's next request."""
    if user_id == actor.id and status is UserStatus.SUSPENDED:
        raise ValidationFailed(status="You can't suspend your own account.")
    _change_status(actor, _get_user(user_id), status)
    db.session.commit()


def bulk_suspend(actor: User, user_ids: Iterable[int]) -> int:
    """Suspend many accounts at once, never your own. Returns how many were newly suspended."""
    wanted = set(user_ids) - {actor.id}
    rows = db.session.scalars(
        select(User)
        .where(User.id.in_(wanted), User.status == UserStatus.ACTIVE)
        .order_by(User.id)
        .with_for_update()
    ).all()
    for user in rows:
        _change_status(actor, user, UserStatus.SUSPENDED)
    db.session.commit()
    return len(rows)


def _change_status(actor: User, user: User, status: UserStatus) -> None:
    if user.status is status:
        return
    user.status = status
    if status is UserStatus.SUSPENDED:
        # Otherwise a stolen session would start working again when the account is reactivated.
        user.session_version += 1
    audit.record(
        "admin.user_status_changed",
        actor_id=actor.id,
        target_type="user",
        target_id=user.id,
        details={"status": status.value},
    )


def _get_user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFoundError("That account doesn't exist.")
    return user


def _find_by_username(username: str, *, for_update: bool = False) -> User | None:
    # Reject the impossible up front: citext comparison can't match anything longer than 30.
    if username_problem(username):
        return None
    query = select(User).where(User.username == username)
    if for_update:
        query = query.with_for_update()
    return db.session.scalar(query)


def _issue_recovery_codes(user_id: int) -> tuple[str, ...]:
    codes = tuple(_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT))
    db.session.add_all(
        RecoveryCode(user_id=user_id, code_hash=_hash_code(user_id, code)) for code in codes
    )
    return codes


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    groups = [raw[i : i + _CODE_GROUP] for i in range(0, _CODE_LENGTH, _CODE_GROUP)]
    return "-".join(groups)


def _find_unused_code(user_id: int, code: str) -> RecoveryCode | None:
    # A row lock, so two requests racing with the same code can't both succeed.
    return db.session.scalar(
        select(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == _hash_code(user_id, code),
            RecoveryCode.used_at.is_(None),
        )
        .with_for_update()
    )


def _hash_code(user_id: int, code: str) -> str:
    """Hash a recovery code.

    A fast hash is right here. Slow hashing protects passwords people choose, which are
    guessable; these are 80 random bits, which aren't, and it means a guess costs the server
    almost nothing.
    """
    normalised = re.sub(r"[\s-]", "", code or "").upper().translate(_CODE_CONFUSABLES)
    return hashlib.sha256(f"{user_id}:{normalised}".encode()).hexdigest()
