"""Password rules. Hashing is covered through real logins in the integration tests."""

from __future__ import annotations

import pytest

from app.security.passwords import MAX_PASSWORD_LENGTH, normalise, password_problem


@pytest.mark.parametrize(
    "password",
    ["correct-horse-battery", "a-long-Passphrase!", "Tr0ub4dor&3xtra", "ünïcödé-pässwörd"],
)
def test_accepts_reasonable_passwords(password):
    assert password_problem(password, "alex") is None


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("", "at least 8"),
        ("seven77", "at least 8"),
        ("x" * (MAX_PASSWORD_LENGTH + 1), "at most"),
        ("password", "too easy"),
        ("PASSWORD", "too easy"),
        ("qwertyuiop", "too easy"),
        ("iloveyou", "too easy"),
        ("11111111", "too easy"),
        ("abababababab", "too easy"),
    ],
)
def test_rejects_weak_passwords(password, fragment):
    assert fragment in password_problem(password, "alex")


def test_rejects_password_containing_the_username():
    assert "username" in password_problem("xx-ALEX-yy-long", "alex")


def test_exactly_the_maximum_is_fine():
    assert password_problem("Ab1-" * (MAX_PASSWORD_LENGTH // 4), "alex") is None


def test_the_same_characters_typed_on_two_devices_match():
    composed = "café-passphrase"
    decomposed = "café-passphrase"
    assert composed != decomposed
    assert normalise(composed) == normalise(decomposed)
