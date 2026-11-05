import random
from decimal import Decimal

import pytest

from app.money import (
    FULL_SHARE_BP,
    MAX_AMOUNT,
    InvalidMoneyError,
    InvalidPercentError,
    allocate,
    basis_points_to_percent,
    even_shares,
    format_aud,
    from_cents,
    percent_to_basis_points,
    to_cents,
    to_money,
    to_percent,
)

D = Decimal


class TestToMoney:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12", "12.00"),
            ("12.5", "12.50"),
            ("12.50", "12.50"),
            ("1.500", "1.50"),
            (" $1,234.50 ", "1234.50"),
            ("1,000,000", "1000000.00"),
            ("$0", "0.00"),
            (".5", "0.50"),
            ("7.", "7.00"),
            ("0.01", "0.01"),
        ],
    )
    def test_parses_plain_dollar_amounts(self, text: str, expected: str) -> None:
        result = to_money(text)
        assert result == D(expected)
        assert result.as_tuple().exponent == -2

    @pytest.mark.parametrize(
        "text",
        [
            "",
            " ",
            "abc",
            "nan",
            "NaN",
            "inf",
            "Infinity",
            "-inf",
            "sNaN",
            "1e3",
            "1E-2",
            "1,23",
            "12,34.5",
            "$$5",
            "5$",
            "1.2.3",
            "--5",
            "0x10",
            "\u0661\u0662",  # Arabic-Indic digits
        ],
    )
    def test_rejects_non_amounts(self, text: str) -> None:
        with pytest.raises(InvalidMoneyError):
            to_money(text)

    def test_rejects_more_than_two_decimals(self) -> None:
        with pytest.raises(InvalidMoneyError, match="2 decimal places"):
            to_money("1.505")

    @pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity"), D("sNaN")])
    def test_rejects_non_finite_decimals(self, value: Decimal) -> None:
        with pytest.raises(InvalidMoneyError):
            to_money(value)

    @pytest.mark.parametrize("value", [1.5, float("nan"), True, None, [1]])
    def test_rejects_floats_and_other_types(self, value: object) -> None:
        with pytest.raises(TypeError):
            to_money(value)  # type: ignore[arg-type]

    def test_negative_needs_opt_in(self) -> None:
        with pytest.raises(InvalidMoneyError, match="negative"):
            to_money("-5")
        assert to_money("-5", allow_negative=True) == D("-5.00")
        assert to_money("\u22123.20", allow_negative=True) == D("-3.20")
        assert to_money("-$3.20", allow_negative=True) == D("-3.20")

    def test_zero_can_be_refused(self) -> None:
        assert to_money("0") == D("0.00")
        with pytest.raises(InvalidMoneyError, match="more than"):
            to_money("0.00", allow_zero=False)

    def test_negative_zero_is_normalised(self) -> None:
        result = to_money(D("-0.00"))
        assert result == 0
        assert not result.is_signed()

    def test_size_limit_fits_numeric_12_2(self) -> None:
        assert to_money(MAX_AMOUNT) == MAX_AMOUNT
        with pytest.raises(InvalidMoneyError, match="too large"):
            to_money("10000000000.00")
        with pytest.raises(InvalidMoneyError, match="too large"):
            to_money("9" * 40)

    def test_accepts_ints_and_decimals(self) -> None:
        assert to_money(7) == D("7.00")
        assert to_money(D("3")) == D("3.00")


class TestCents:
    @pytest.mark.parametrize("amount", ["0.00", "0.01", "12.34", "-5.67", "9999999999.99"])
    def test_round_trip(self, amount: str) -> None:
        assert from_cents(to_cents(D(amount))) == D(amount)

    def test_to_cents_values(self) -> None:
        assert to_cents(D("12.34")) == 1234
        assert to_cents(D("-0.05")) == -5
        assert to_cents(D("3")) == 300

    def test_to_cents_rejects_fractions_of_a_cent(self) -> None:
        with pytest.raises(ValueError, match="whole number of cents"):
            to_cents(D("0.005"))
        with pytest.raises(ValueError, match="whole number of cents"):
            to_cents(D("NaN"))

    def test_from_cents_has_two_places(self) -> None:
        assert str(from_cents(5)) == "0.05"
        assert str(from_cents(100)) == "1.00"
        assert str(from_cents(-250)) == "-2.50"

    def test_from_cents_rejects_non_ints(self) -> None:
        with pytest.raises(TypeError):
            from_cents(1.0)  # type: ignore[arg-type]


class TestFormatAud:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (D("0"), "$0.00"),
            (D("-0.00"), "$0.00"),
            (D("1234.5"), "$1,234.50"),
            (D("1000000"), "$1,000,000.00"),
            (D("-3"), "\u2212$3.00"),
            (D("0.05"), "$0.05"),
            (12, "$12.00"),
        ],
    )
    def test_formats_en_au(self, amount: Decimal, expected: str) -> None:
        assert format_aud(amount) == expected

    def test_plain_minus_for_csv(self) -> None:
        assert format_aud(D("-3.5"), minus="-") == "-$3.50"


class TestPercent:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("50", "50.00"),
            ("33.33", "33.33"),
            ("100", "100.00"),
            ("0", "0.00"),
            ("12.5%", "12.50"),
            (".5", "0.50"),
            (D("25"), "25.00"),
            (40, "40.00"),
        ],
    )
    def test_valid(self, value: str | Decimal | int, expected: str) -> None:
        assert to_percent(value) == D(expected)

    @pytest.mark.parametrize(
        "value", ["-1", "100.01", "101", "33.333", "abc", "", "nan", "1e2", "\u0665\u0660"]
    )
    def test_invalid(self, value: str) -> None:
        with pytest.raises(InvalidPercentError):
            to_percent(value)

    def test_rejects_non_finite_and_floats(self) -> None:
        with pytest.raises(InvalidPercentError):
            to_percent(D("NaN"))
        with pytest.raises(TypeError):
            to_percent(50.0)  # type: ignore[arg-type]

    def test_basis_points(self) -> None:
        assert percent_to_basis_points(D("33.33")) == 3333
        assert percent_to_basis_points(D("100")) == FULL_SHARE_BP
        assert basis_points_to_percent(3334) == D("33.34")


class TestAllocate:
    def test_even_thirds_of_a_dollar(self) -> None:
        assert allocate(100, [1, 1, 1]) == [34, 33, 33]

    def test_one_cent_three_ways(self) -> None:
        assert allocate(1, [1, 1, 1]) == [1, 0, 0]

    def test_losses_split_symmetrically(self) -> None:
        assert allocate(-100, [1, 1, 1]) == [-34, -33, -33]

    def test_zero_total(self) -> None:
        assert allocate(0, [3, 2]) == [0, 0]

    def test_with_explicit_denominator(self) -> None:
        assert allocate(7500, [5000, 5000], FULL_SHARE_BP) == [3750, 3750]

    def test_weights_short_of_denominator_leave_the_rest(self) -> None:
        # Shares totalling 90% hand out 90% of the total.
        assert allocate(10000, [5000, 4000], FULL_SHARE_BP) == [5000, 4000]

    def test_largest_remainder_wins_the_spare_cent(self) -> None:
        # 10 cents at 15% / 85% → 1.5 and 8.5; tie goes to the first.
        assert allocate(10, [1500, 8500], FULL_SHARE_BP) == [2, 8]
        # 10 cents at 14% / 86% → 1.4 and 8.6; 8.6 has the bigger remainder.
        assert allocate(10, [1400, 8600], FULL_SHARE_BP) == [1, 9]

    def test_zero_weight_gets_nothing(self) -> None:
        assert allocate(101, [0, 1, 1]) == [0, 51, 50]

    def test_empty(self) -> None:
        assert allocate(100, [], 1) == []

    def test_errors(self) -> None:
        with pytest.raises(ValueError, match="can't be negative"):
            allocate(100, [1, -1])
        with pytest.raises(ValueError, match="denominator must be positive"):
            allocate(100, [0, 0])
        with pytest.raises(ValueError, match="denominator must be positive"):
            allocate(100, [1], 0)

    def test_parts_always_sum_to_total_when_weights_are_complete(self) -> None:
        rng = random.Random(20261105)
        for _ in range(2000):
            count = rng.randint(1, 8)
            weights = allocate(FULL_SHARE_BP, [rng.randint(0, 50) + 1 for _ in range(count)])
            total = rng.randint(-10_000_000, 10_000_000)
            parts = allocate(total, weights, FULL_SHARE_BP)
            assert sum(parts) == total
            # No part is more than one unit away from its exact quota.
            for part, weight in zip(parts, weights, strict=True):
                assert abs(part - total * weight / FULL_SHARE_BP) < 1


class TestEvenShares:
    def test_three_people(self) -> None:
        assert even_shares(3) == [D("33.34"), D("33.33"), D("33.33")]

    def test_one_person(self) -> None:
        assert even_shares(1) == [D("100.00")]

    @pytest.mark.parametrize("count", range(1, 21))
    def test_always_totals_exactly_100(self, count: int) -> None:
        assert sum(even_shares(count)) == D("100.00")

    def test_needs_someone(self) -> None:
        with pytest.raises(ValueError, match="at least one person"):
            even_shares(0)
