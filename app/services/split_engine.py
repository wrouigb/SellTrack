"""Profit split engine (cash basis).

Pure calculation with no database or Flask imports, so every rule can be
tested directly. ``app.services.split`` loads the current period from the
database and passes it in here.

The rules, for the current period (everything since the last settle-up):

    Money in         = all sale money received
    Upfront costs    = all upfront costs paid
    Profit to share  = Money in − Upfront costs      (can be negative; losses are shared too)

    For each person:
      Gets back       = upfront costs they paid       (paid back before anything is split)
      Share           = Profit to share × their %     (largest-remainder rounding, in cents)
      Should end with = Gets back + Share
      Holding         = sale money they received + payments received − payments made
      Balance         = Should end with − Holding     (+ owed to them, − they owe)

When shares total exactly 100% and every sale has a receiver, balances total
exactly $0.00 and :func:`suggest_transfers` finds at most ``n − 1`` payments
that bring everyone to zero. Per-item product costs are deliberately *not*
used here: log the stock you buy as an upfront cost instead, so whoever paid
for it is paid back (plan section 3.4, option A).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.money import (
    FULL_SHARE_BP,
    allocate,
    basis_points_to_percent,
    from_cents,
    percent_to_basis_points,
    to_cents,
)

SNAPSHOT_VERSION = 1


class SplitInputError(ValueError):
    """The inputs are inconsistent (a programming error, not a user mistake)."""


class Issue(StrEnum):
    """Why the period can't be settled yet. Shown to the user, in this order."""

    NO_PEOPLE = "no_people"
    SHARES_NOT_100 = "shares_not_100"
    UNASSIGNED_SALES = "unassigned_sales"


@dataclass(frozen=True, slots=True)
class Person:
    id: int
    name: str
    share_pct: Decimal


@dataclass(frozen=True, slots=True)
class SaleMoney:
    """Money from one sale. ``received_by=None`` means "the usual receiver"."""

    amount: Decimal
    received_by: int | None = None


@dataclass(frozen=True, slots=True)
class UpfrontCost:
    amount: Decimal
    paid_by: int


@dataclass(frozen=True, slots=True)
class Payment:
    """Money one person handed another, e.g. part-paying a debt."""

    amount: Decimal
    from_id: int
    to_id: int


@dataclass(frozen=True, slots=True)
class PersonResult:
    person_id: int
    name: str
    share_pct: Decimal
    gets_back: Decimal
    share_of_profit: Decimal
    should_end_with: Decimal
    holding: Decimal
    balance: Decimal

    @property
    def is_owed(self) -> bool:
        return self.balance > 0

    @property
    def owes(self) -> bool:
        return self.balance < 0


@dataclass(frozen=True, slots=True)
class Transfer:
    from_id: int
    from_name: str
    to_id: int
    to_name: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SplitResult:
    money_in: Decimal
    upfront_total: Decimal
    profit_to_share: Decimal
    shares_total: Decimal
    unassigned_money_in: Decimal
    people: tuple[PersonResult, ...]
    transfers: tuple[Transfer, ...]
    issues: tuple[Issue, ...]

    @property
    def can_settle(self) -> bool:
        """True when the balances are complete and add up to zero."""
        return not self.issues

    @property
    def has_debts(self) -> bool:
        return any(p.balance != 0 for p in self.people)

    def person(self, person_id: int) -> PersonResult:
        for result in self.people:
            if result.person_id == person_id:
                return result
        raise KeyError(person_id)

    def to_snapshot(self) -> dict[str, Any]:
        """JSON-safe record stored on a settlement. Amounts are strings."""
        return {
            "version": SNAPSHOT_VERSION,
            "money_in": str(self.money_in),
            "upfront_total": str(self.upfront_total),
            "profit_to_share": str(self.profit_to_share),
            "shares_total": str(self.shares_total),
            "people": [
                {
                    "person_id": p.person_id,
                    "name": p.name,
                    "share_pct": str(p.share_pct),
                    "gets_back": str(p.gets_back),
                    "share_of_profit": str(p.share_of_profit),
                    "should_end_with": str(p.should_end_with),
                    "holding": str(p.holding),
                    "balance": str(p.balance),
                }
                for p in self.people
            ],
            "transfers": [
                {
                    "from_id": t.from_id,
                    "from_name": t.from_name,
                    "to_id": t.to_id,
                    "to_name": t.to_name,
                    "amount": str(t.amount),
                }
                for t in self.transfers
            ],
        }


def calculate(
    people: Sequence[Person],
    sales: Iterable[SaleMoney] = (),
    costs: Iterable[UpfrontCost] = (),
    payments: Iterable[Payment] = (),
    *,
    default_receiver_id: int | None = None,
) -> SplitResult:
    """Work out who should end up with what, and who owes whom.

    ``people`` is used in the order given (that order is also the tie-break
    for rounding and transfers, so pass a stable order such as creation order).
    """
    index = _index_people(people)
    if default_receiver_id is not None and default_receiver_id not in index:
        raise SplitInputError(f"default receiver {default_receiver_id} is not in the split")

    count = len(people)
    gets_back = [0] * count
    holding = [0] * count
    money_in = 0
    unassigned = 0
    upfront_total = 0

    for sale in sales:
        cents = _cents(sale.amount, what="sale amount", allow_zero=True)
        money_in += cents
        receiver = sale.received_by if sale.received_by is not None else default_receiver_id
        if receiver is None:
            unassigned += cents
        else:
            holding[_position(index, receiver, "sale receiver")] += cents

    for cost in costs:
        cents = _cents(cost.amount, what="upfront cost", allow_zero=False)
        upfront_total += cents
        gets_back[_position(index, cost.paid_by, "upfront cost payer")] += cents

    for payment in payments:
        cents = _cents(payment.amount, what="payment", allow_zero=False)
        if payment.from_id == payment.to_id:
            raise SplitInputError("a payment must be between two different people")
        holding[_position(index, payment.from_id, "payment sender")] -= cents
        holding[_position(index, payment.to_id, "payment receiver")] += cents

    share_bp = [percent_to_basis_points(p.share_pct) for p in people]
    profit = money_in - upfront_total
    shares = allocate(profit, share_bp, FULL_SHARE_BP) if people else []

    results = tuple(
        PersonResult(
            person_id=p.id,
            name=p.name,
            share_pct=basis_points_to_percent(share_bp[i]),
            gets_back=from_cents(gets_back[i]),
            share_of_profit=from_cents(shares[i]),
            should_end_with=from_cents(gets_back[i] + shares[i]),
            holding=from_cents(holding[i]),
            balance=from_cents(gets_back[i] + shares[i] - holding[i]),
        )
        for i, p in enumerate(people)
    )

    issues: list[Issue] = []
    if not people:
        issues.append(Issue.NO_PEOPLE)
    elif sum(share_bp) != FULL_SHARE_BP:
        issues.append(Issue.SHARES_NOT_100)
    if unassigned:
        issues.append(Issue.UNASSIGNED_SALES)

    transfers: tuple[Transfer, ...] = ()
    if not issues:
        balances = [to_cents(r.balance) for r in results]
        if sum(balances) != 0:  # pragma: no cover - guaranteed by the maths above
            raise AssertionError("balances must total zero when shares total 100%")
        transfers = tuple(
            Transfer(
                from_id=people[debtor].id,
                from_name=people[debtor].name,
                to_id=people[creditor].id,
                to_name=people[creditor].name,
                amount=from_cents(cents),
            )
            for debtor, creditor, cents in suggest_transfers(balances)
        )

    return SplitResult(
        money_in=from_cents(money_in),
        upfront_total=from_cents(upfront_total),
        profit_to_share=from_cents(profit),
        shares_total=basis_points_to_percent(sum(share_bp)),
        unassigned_money_in=from_cents(unassigned),
        people=results,
        transfers=transfers,
        issues=tuple(issues),
    )


def suggest_transfers(balances: Sequence[int]) -> list[tuple[int, int, int]]:
    """Payments ``(from_index, to_index, cents)`` that bring every balance to zero.

    Greedy: the person who owes the most pays the person owed the most, as
    much as one of them needs, then repeat. Each step settles at least one
    person, so there are at most ``n − 1`` payments. Ties go to the earlier
    person, so the suggestion is stable between page loads.
    """
    if sum(balances) != 0:
        raise ValueError("balances must total zero")
    owed = {i: b for i, b in enumerate(balances) if b > 0}
    owing = {i: -b for i, b in enumerate(balances) if b < 0}
    transfers: list[tuple[int, int, int]] = []
    while owing:
        debtor = max(owing, key=lambda i: (owing[i], -i))
        creditor = max(owed, key=lambda i: (owed[i], -i))
        amount = min(owing[debtor], owed[creditor])
        transfers.append((debtor, creditor, amount))
        for bucket, who in ((owing, debtor), (owed, creditor)):
            bucket[who] -= amount
            if bucket[who] == 0:
                del bucket[who]
    return transfers


def _index_people(people: Sequence[Person]) -> dict[int, int]:
    index: dict[int, int] = {}
    for position, person in enumerate(people):
        if person.id in index:
            raise SplitInputError(f"person {person.id} appears twice")
        index[person.id] = position
    return index


def _position(index: dict[int, int], person_id: int, role: str) -> int:
    try:
        return index[person_id]
    except KeyError:
        raise SplitInputError(f"{role} {person_id} is not in the split") from None


def _cents(amount: Decimal, *, what: str, allow_zero: bool) -> int:
    try:
        cents = to_cents(amount)
    except ValueError as exc:
        raise SplitInputError(f"{what} {amount!r} is not a whole number of cents") from exc
    if cents < 0 or (cents == 0 and not allow_zero):
        raise SplitInputError(f"{what} must be {'zero or more' if allow_zero else 'positive'}")
    return cents
