"""Password rules and hashing (Argon2id)."""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import current_app

MIN_PASSWORD_LENGTH = 8
#: A cap on what gets hashed, so a huge "password" can't be used to burn CPU.
MAX_PASSWORD_LENGTH = 128
_MIN_DISTINCT_CHARS = 4
_BLOCKLIST_PATH = Path(__file__).with_name("common_passwords.txt")


def normalise(password: str) -> str:
    """The same characters typed on two devices can be encoded differently; hash just one form."""
    return unicodedata.normalize("NFKC", password)


def password_problem(password: str, username: str = "") -> str | None:
    """Why ``password`` isn't acceptable, as a message for the user, or ``None`` if it's fine."""
    password = normalise(password)
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    lowered = password.casefold()
    if lowered in _common_passwords() or len(set(lowered)) < _MIN_DISTINCT_CHARS:
        return "That password is too easy to guess. Try a few unrelated words."
    if username and username.casefold() in lowered:
        return "Your password can't contain your username."
    return None


def hash_password(password: str) -> str:
    return _hasher().hash(normalise(password))


def verify_password(password_hash: str, password: str) -> bool:
    """True if ``password`` matches. Malformed stored hashes count as no match."""
    if len(password) > MAX_PASSWORD_LENGTH * 4:  # at most 4 bytes per character when normalised
        return False
    try:
        return _hasher().verify(password_hash, normalise(password))
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker settings than the current ones."""
    return _hasher().check_needs_rehash(password_hash)


def burn_verification_time(password: str) -> None:
    """Spend as long as a real check, for a username that doesn't exist.

    Without this, a failed login returns noticeably faster for unknown usernames, which would
    tell an attacker which accounts exist.
    """
    verify_password(_dummy_hash(), password)


def _hasher() -> PasswordHasher:
    config = current_app.config
    return _hasher_for(
        config["ARGON2_TIME_COST"], config["ARGON2_MEMORY_COST_KIB"], config["ARGON2_PARALLELISM"]
    )


@lru_cache(maxsize=4)
def _hasher_for(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)


def _dummy_hash() -> str:
    config = current_app.config
    return _dummy_hash_for(
        config["ARGON2_TIME_COST"], config["ARGON2_MEMORY_COST_KIB"], config["ARGON2_PARALLELISM"]
    )


@lru_cache(maxsize=4)
def _dummy_hash_for(time_cost: int, memory_cost: int, parallelism: int) -> str:
    return _hasher_for(time_cost, memory_cost, parallelism).hash("no-account-has-this-password")


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    lines = _BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))
