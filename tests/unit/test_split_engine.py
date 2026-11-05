import json
import random
from decimal import Decimal

import pytest

from app.money import (
    FULL_SHARE_BP,
    InvalidPercentError,
    allocate,
    basis_points_to_percent,
    even_shares,
    from_cents,
)
from app.services.split_engine import (
    Issue,
    Payment,
    Person,
    SaleMoney,
    SplitInputError,
    SplitResult,
    UpfrontCost,
    calculate,
    suggest_transfers,
)

D = Decimal
ALEX, SAM, JO = 1, 2, 3


def pair(alex: str = "50", sam: str = "50") -> list[Person]:
    return [Person(ALEX, "Alex", D(alex)), Person(SAM, "Sam", D(sam))]


def transfers_of(result: SplitResult) -> list[tuple[str, str, str]]:
    return [(t.from_name, t.to_name, str(t.amount)) for t in result.transfers]


class TestWorkedExample:
    """Plan section 3.3: the example agreed with the user."""

    @pytest.fixture
    def result(self) -> SplitResult:
        return calculate(
            pair(),
            sales=[SaleMoney(D("80.00"), SAM), SaleMoney(D("40.00"), ALEX)],
            costs=[UpfrontCost(D("35.00"), ALEX), UpfrontCost(D("10.00"), SAM)],
        )

    def test_totals(self, result: SplitResult) -> None:
        assert result.money_in == D("120.00")
        assert result.upfront_total == D("45.00")
        assert result.profit_to_share == D("75.00")
        assert result.shares_total == D("100.00")

    def test_each_person(self, result: SplitResult) -> None:
        alex, sam = result.person(ALEX), result.person(SAM)
        assert (alex.gets_back, alex.share_of_profit, alex.should_end_with) == (
            D("35.00"),
            D("37.50"),
            D("72.50"),
        )
        assert (sam.gets_back, sam.share_of_profit, sam.should_end_with) == (
            D("10.00"),
            D("37.50"),
            D("47.50"),
        )
        assert (alex.holding, alex.balance) == (D("40.00"), D("32.50"))
        assert (sam.holding, sam.balance) == (D("80.00"), D("-32.50"))
        assert alex.is_owed
        assert sam.owes

    def test_sam_pays_alex(self, result: SplitResult) -> None:
        assert result.can_settle
        assert result.has_debts
        assert transfers_of(result) == [("Sam", "Alex", "32.50")]

    def test_part_payment_reduces_the_debt(self) -> None:
        result = calculate(
            pair(),
            sales=[SaleMoney(D("80.00"), SAM), SaleMoney(D("40.00"), ALEX)],
            costs=[UpfrontCost(D("35.00"), ALEX), UpfrontCost(D("10.00"), SAM)],
            payments=[Payment(D("20.00"), from_id=SAM, to_id=ALEX)],
        )
        assert result.person(ALEX).balance == D("12.50")
        assert result.person(SAM).balance == D("-12.50")
        assert transfers_of(result) == [("Sam", "Alex", "12.50")]

    def test_paying_in_full_clears_everything(self) -> None:
        result = calculate(
            pair(),
            sales=[SaleMoney(D("80.00"), SAM), SaleMoney(D("40.00"), ALEX)],
            costs=[UpfrontCost(D("35.00"), ALEX), UpfrontCost(D("10.00"), SAM)],
            payments=[Payment(D("32.50"), from_id=SAM, to_id=ALEX)],
        )
        assert not result.has_debts
        assert result.transfers == ()
        assert result.can_settle
        # After squaring up, everyone holds exactly what they should end with.
        for person in result.people:
            assert person.holding == person.should_end_with


class TestLossesAndRounding:
    def test_loss_is_shared_by_percentage(self) -> None:
        # Stock cost more than it has sold for so far.
        result = calculate(
            pair(),
            sales=[SaleMoney(D("30.00"), SAM)],
            costs=[UpfrontCost(D("35.00"), ALEX)],
        )
        assert result.profit_to_share == D("-5.00")
        assert result.person(ALEX).share_of_profit == D("-2.50")
        assert result.person(ALEX).should_end_with == D("32.50")
        assert result.person(SAM).should_end_with == D("-2.50")
        assert transfers_of(result) == [("Sam", "Alex", "32.50")]

    def test_three_way_split_never_loses_a_cent(self) -> None:
        shares = even_shares(3)
        people = [Person(i + 1, n, s) for i, (n, s) in enumerate(zip("ABC", shares, strict=True))]
        result = calculate(people, sales=[SaleMoney(D("100.00"), 1)])
        assert [p.share_of_profit for p in result.people] == [D("33.34"), D("33.33"), D("33.33")]
        assert sum(p.share_of_profit for p in result.people) == D("100.00")
        assert transfers_of(result) == [("A", "B", "33.33"), ("A", "C", "33.33")]

    def test_one_cent_profit(self) -> None:
        people = [
            Person(i + 1, n, s) for i, (n, s) in enumerate(zip("ABC", even_shares(3), strict=True))
        ]
        result = calculate(people, sales=[SaleMoney(D("0.01"), 2)])
        assert [p.share_of_profit for p in result.people] == [D("0.01"), D("0.00"), D("0.00")]
        assert transfers_of(result) == [("B", "A", "0.01")]

    def test_uneven_shares(self) -> None:
        result = calculate(pair("70", "30"), sales=[SaleMoney(D("10.01"), ALEX)])
        assert result.person(ALEX).share_of_profit == D("7.01")
        assert result.person(SAM).share_of_profit == D("3.00")


class TestReceivers:
    def test_sales_default_to_the_usual_receiver(self) -> None:
        result = calculate(
            pair(),
            sales=[SaleMoney(D("50.00")), SaleMoney(D("10.00"), ALEX)],
            default_receiver_id=SAM,
        )
        assert result.person(SAM).holding == D("50.00")
        assert result.person(ALEX).holding == D("10.00")
        assert transfers_of(result) == [("Sam", "Alex", "20.00")]

    def test_sales_with_no_receiver_block_settling(self) -> None:
        result = calculate(pair(), sales=[SaleMoney(D("50.00"))])
        assert result.issues == (Issue.UNASSIGNED_SALES,)
        assert result.unassigned_money_in == D("50.00")
        assert result.money_in == D("50.00")
        assert not result.can_settle
        assert result.transfers == ()

    def test_free_items_are_allowed(self) -> None:
        result = calculate(pair(), sales=[SaleMoney(D("0.00"), ALEX)])
        assert result.money_in == D("0.00")
        assert not result.has_debts


class TestIssues:
    def test_no_people(self) -> None:
        result = calculate([], sales=[SaleMoney(D("12.00"))])
        assert Issue.NO_PEOPLE in result.issues
        assert result.people == ()
        assert result.money_in == D("12.00")
        assert not result.can_settle

    def test_shares_under_100(self) -> None:
        result = calculate(pair("50", "40"), sales=[SaleMoney(D("100.00"), ALEX)])
        assert result.issues == (Issue.SHARES_NOT_100,)
        assert result.shares_total == D("90.00")
        # Shares are still shown so the user can see what's missing.
        assert result.person(ALEX).share_of_profit == D("50.00")
        assert result.person(SAM).share_of_profit == D("40.00")
        assert result.transfers == ()

    def test_shares_over_100(self) -> None:
        result = calculate(pair("60", "60"))
        assert result.issues == (Issue.SHARES_NOT_100,)
        assert result.shares_total == D("120.00")

    def test_all_shares_zero(self) -> None:
        result = calculate(pair("0", "0"), sales=[SaleMoney(D("10.00"), ALEX)])
        assert result.issues == (Issue.SHARES_NOT_100,)
        assert [p.share_of_profit for p in result.people] == [D("0.00"), D("0.00")]

    def test_empty_period_can_be_settled(self) -> None:
        result = calculate(pair())
        assert result.can_settle
        assert not result.has_debts
        assert result.profit_to_share == D("0.00")

    def test_zero_share_person_still_gets_their_costs_back(self) -> None:
        people = [*pair("100", "0")]
        result = calculate(
            people,
            sales=[SaleMoney(D("100.00"), ALEX)],
            costs=[UpfrontCost(D("20.00"), SAM)],
        )
        assert result.person(SAM).share_of_profit == D("0.00")
        assert result.person(SAM).should_end_with == D("20.00")
        assert transfers_of(result) == [("Alex", "Sam", "20.00")]


class TestInputChecks:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sales": [SaleMoney(D("1.00"), 99)]},
            {"costs": [UpfrontCost(D("1.00"), 99)]},
            {"payments": [Payment(D("1.00"), ALEX, 99)]},
            {"payments": [Payment(D("1.00"), ALEX, ALEX)]},
            {"sales": [SaleMoney(D("-1.00"), ALEX)]},
            {"sales": [SaleMoney(D("1.005"), ALEX)]},
            {"sales": [SaleMoney(D("NaN"), ALEX)]},
            {"costs": [UpfrontCost(D("0.00"), ALEX)]},
            {"payments": [Payment(D("0.00"), ALEX, SAM)]},
            {"default_receiver_id": 99},
        ],
    )
    def test_rejects_inconsistent_input(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(SplitInputError):
            calculate(pair(), **kwargs)  # type: ignore[arg-type]

    def test_rejects_duplicate_people(self) -> None:
        with pytest.raises(SplitInputError):
            calculate([Person(1, "A", D("50")), Person(1, "B", D("50"))])

    def test_rejects_bad_share(self) -> None:
        with pytest.raises(InvalidPercentError, match="2 decimal places"):
            calculate([Person(1, "A", D("33.333"))])


class TestSnapshot:
    def test_is_json_safe_and_complete(self) -> None:
        result = calculate(
            pair(),
            sales=[SaleMoney(D("80.00"), SAM), SaleMoney(D("40.00"), ALEX)],
            costs=[UpfrontCost(D("35.00"), ALEX), UpfrontCost(D("10.00"), SAM)],
        )
        snapshot = json.loads(json.dumps(result.to_snapshot()))
        assert snapshot["version"] == 1
        assert snapshot["profit_to_share"] == "75.00"
        assert [p["balance"] for p in snapshot["people"]] == ["32.50", "-32.50"]
        assert snapshot["transfers"] == [
            {
                "from_id": SAM,
                "from_name": "Sam",
                "to_id": ALEX,
                "to_name": "Alex",
                "amount": "32.50",
            }
        ]


class TestSuggestTransfers:
    def test_four_people(self) -> None:
        assert suggest_transfers([-5000, -1000, 4000, 2000]) == [
            (0, 2, 4000),
            (0, 3, 1000),
            (1, 3, 1000),
        ]

    def test_nothing_owed(self) -> None:
        assert suggest_transfers([0, 0, 0]) == []

    def test_must_balance(self) -> None:
        with pytest.raises(ValueError, match="total zero"):
            suggest_transfers([100, -50])


def random_period(
    rng: random.Random,
) -> tuple[list[Person], list[SaleMoney], list[UpfrontCost], list[Payment]]:
    count = rng.randint(1, 6)
    weights = allocate(FULL_SHARE_BP, [rng.randint(1, 9) for _ in range(count)])
    people = [Person(100 + i, f"P{i}", basis_points_to_percent(w)) for i, w in enumerate(weights)]
    ids = [p.id for p in people]

    def amount(low: int, high: int) -> Decimal:
        return from_cents(rng.randint(low, high))

    sales = [
        SaleMoney(amount(0, 50_000), rng.choice([*ids, None])) for _ in range(rng.randint(0, 25))
    ]
    costs = [UpfrontCost(amount(1, 80_000), rng.choice(ids)) for _ in range(rng.randint(0, 6))]
    payments = []
    if count > 1:
        for _ in range(rng.randint(0, 4)):
            a, b = rng.sample(ids, 2)
            payments.append(Payment(amount(1, 20_000), a, b))
    return people, sales, costs, payments


def test_invariants_hold_for_random_periods() -> None:
    rng = random.Random(5112026)
    for _ in range(1500):
        people, sales, costs, payments = random_period(rng)
        default = rng.choice([p.id for p in people])
        result = calculate(people, sales, costs, payments, default_receiver_id=default)

        assert result.can_settle
        assert result.profit_to_share == result.money_in - result.upfront_total
        assert sum(p.balance for p in result.people) == 0
        assert sum(p.should_end_with for p in result.people) == result.money_in
        assert sum(p.gets_back for p in result.people) == result.upfront_total
        assert len(result.transfers) <= max(len(people) - 1, 0)
        assert all(t.amount > 0 and t.from_id != t.to_id for t in result.transfers)

        # Settling up = making the suggested payments. Afterwards nobody owes anything,
        # and a fresh calculation suggests no further transfers.
        settled = calculate(
            people,
            sales,
            costs,
            [*payments, *(Payment(t.amount, t.from_id, t.to_id) for t in result.transfers)],
            default_receiver_id=default,
        )
        assert not settled.has_debts
        assert settled.transfers == ()
