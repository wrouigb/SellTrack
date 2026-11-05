"""Money and percentage helpers.

All money in SellTrack is Australian dollars. In Python it is a ``Decimal``
with exactly two decimal places; in Postgres it is ``numeric(12, 2)``.
Floats are never used for money: ``0.7 * 3`` is ``2.0999999999999996``.

Anything that has to add up exactly (profit shares, even splits) is worked
out in whole cents with :func:`allocate`, which hands out leftover cents by
the largest-remainder method so the parts always sum to the total.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction

CENT = Decimal("0.01")
#: Largest amount that fits ``numeric(12, 2)``.
MAX_AMOUNT = Decimal("9999999999.99")
#: Typographic minus sign used for display; screen readers announce it as "minus".
MINUS = "\u2212"
#: Percentages are stored with two decimals, so 100% is 10,000 basis points.
FULL_SHARE_BP = 10_000

# Accepts "12", "12.5", "$1,234.50", "-$3", "−3.00", ".50". Rejects "1e3", "nan",
# "inf", "1,23" and anything else that isn't plainly a number of dollars.
_MONEY_RE = re.compile(
    r"""
    ^(?P<sign>[-\u2212])?
    \$?
    (?:
        (?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d*))?
      | \.(?P<frac_only>\d+)
    )$
    """,
    # ASCII: \d must not match other scripts' digits, e.g. Arabic-Indic "\u0661\u0662".
    re.VERBOSE | re.ASCII,
)
_PERCENT_RE = re.compile(r"^(?P<int>\d+)(?:\.(?P<frac>\d*))?%?$|^\.(?P<frac_only>\d+)%?$", re.ASCII)


class InvalidMoneyError(ValueError):
    """A value can't be used as an amount of money. The message is user-facing."""


class InvalidPercentError(ValueError):
    """A value can't be used as a share percentage. The message is user-facing."""


def to_money(
    value: Decimal | int | str,
    *,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    """Return ``value`` as a two-decimal ``Decimal``, or raise :class:`InvalidMoneyError`.

    Strings may include a leading ``$``, thousands commas and surrounding
    whitespace. More than two decimal places is an error rather than being
    silently rounded ("1.500" is fine, "1.505" is not).
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"money must be Decimal, int or str, not {type(value).__name__}")

    if isinstance(value, str):
        amount = _parse_money_text(value)
    elif isinstance(value, int):
        amount = Decimal(value)
    else:
        if not value.is_finite():
            raise InvalidMoneyError("Enter an amount in dollars and cents.")
        amount = value

    # Check size before quantizing: quantize() raises on numbers wider than
    # the Decimal context precision.
    if abs(amount) > MAX_AMOUNT:
        raise InvalidMoneyError("That amount is too large.")
    quantized = amount.quantize(CENT)
    if quantized != amount:
        raise InvalidMoneyError("Use at most 2 decimal places.")
    if quantized.is_zero():
        quantized = abs(quantized)  # normalise "-0.00"
    if quantized < 0 and not allow_negative:
        raise InvalidMoneyError("The amount can't be negative.")
    if quantized.is_zero() and not allow_zero:
        raise InvalidMoneyError("The amount must be more than $0.00.")
    return quantized


def _parse_money_text(text: str) -> Decimal:
    match = _MONEY_RE.match(text.strip())
    if match is None:
        raise InvalidMoneyError("Enter an amount in dollars and cents, like 12.50.")
    whole = (match.group("int") or "0").replace(",", "")
    frac = match.group("frac") or match.group("frac_only") or ""
    sign = "-" if match.group("sign") else ""
    try:
        return Decimal(f"{sign}{whole}.{frac or '0'}")
    except InvalidOperation as exc:  # pragma: no cover - regex already guarantees digits
        raise InvalidMoneyError("Enter an amount in dollars and cents, like 12.50.") from exc


def to_cents(amount: Decimal) -> int:
    """Convert a two-decimal amount to whole cents."""
    if not amount.is_finite() or amount.quantize(CENT) != amount:
        raise ValueError(f"{amount!r} is not a whole number of cents")
    return int(amount.scaleb(2))


def from_cents(cents: int) -> Decimal:
    """Convert whole cents back to a two-decimal amount."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError("cents must be an int")
    return Decimal(cents).scaleb(-2).quantize(CENT)


def format_aud(amount: Decimal | int, *, minus: str = MINUS) -> str:
    """Format for display in en-AU style: ``$1,234.50`` or ``−$3.00``.

    Pass ``minus="-"`` for plain-text output such as CSV files.
    """
    value = Decimal(amount).quantize(CENT)
    sign = minus if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def to_percent(value: Decimal | int | str) -> Decimal:
    """Return a share percentage between 0 and 100 with at most two decimals."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError(f"percent must be Decimal, int or str, not {type(value).__name__}")
    if isinstance(value, str):
        match = _PERCENT_RE.match(value.strip())
        if match is None:
            raise InvalidPercentError("Enter a share between 0 and 100, like 50 or 33.33.")
        whole = match.group("int") or "0"
        frac = match.group("frac") or match.group("frac_only") or ""
        pct = Decimal(f"{whole}.{frac or '0'}")
    else:
        pct = Decimal(value)
        if not pct.is_finite():
            raise InvalidPercentError("Enter a share between 0 and 100, like 50 or 33.33.")

    if pct < 0 or pct > 100:
        raise InvalidPercentError("A share must be between 0 and 100.")
    quantized = pct.quantize(CENT)
    if quantized != pct:
        raise InvalidPercentError("Use at most 2 decimal places for a share.")
    return abs(quantized) if quantized.is_zero() else quantized


def percent_to_basis_points(pct: Decimal) -> int:
    """``Decimal("33.33")`` → ``3333``. 100% is :data:`FULL_SHARE_BP`."""
    return int(to_percent(pct).scaleb(2))


def basis_points_to_percent(bp: int) -> Decimal:
    return Decimal(bp).scaleb(-2).quantize(CENT)


def allocate(total: int, weights: Sequence[int], denominator: int | None = None) -> list[int]:
    """Split ``total`` (whole units, e.g. cents) in proportion to ``weights``.

    Each part is ``total * weight / denominator`` rounded so that the parts sum
    to ``total * sum(weights) / denominator`` rounded half-up. When the weights
    add up to the denominator, the parts sum to ``total`` exactly. Leftover
    units go to the parts with the largest remainders; ties go to the earlier
    part, so results are deterministic. Negative totals (losses) are split
    symmetrically.

    >>> allocate(100, [1, 1, 1])
    [34, 33, 33]
    >>> allocate(-100, [1, 1, 1])
    [-34, -33, -33]
    """
    if any(w < 0 for w in weights):
        raise ValueError("weights can't be negative")
    weight_sum = sum(weights)
    denom = weight_sum if denominator is None else denominator
    if denom <= 0:
        raise ValueError("denominator must be positive (do the weights add up to zero?)")
    if not weights:
        return []

    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    quotas = [Fraction(magnitude * w, denom) for w in weights]
    floors = [q.numerator // q.denominator for q in quotas]
    target = _round_half_up(Fraction(magnitude * weight_sum, denom))
    leftover = target - sum(floors)

    by_remainder = sorted(range(len(quotas)), key=lambda i: (-(quotas[i] - floors[i]), i))
    for i in by_remainder[:leftover]:
        floors[i] += 1
    return [sign * part for part in floors]


def _round_half_up(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    return floor + 1 if value - floor >= Fraction(1, 2) else floor


def even_shares(count: int) -> list[Decimal]:
    """Equal percentages for ``count`` people that total exactly 100.00.

    >>> [str(p) for p in even_shares(3)]
    ['33.34', '33.33', '33.33']
    """
    if count < 1:
        raise ValueError("need at least one person")
    return [basis_points_to_percent(bp) for bp in allocate(FULL_SHARE_BP, [1] * count)]
