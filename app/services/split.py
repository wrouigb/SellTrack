"""The profit split, backed by the database.

``split_engine`` holds the rules and knows nothing about storage. This module works out what
goes into it: everything in the current period, which is every row with ``settlement_id IS NULL``.

* Settling up stamps those rows with a new settlement and saves a permanent snapshot, which
  resets the tally to $0. Rows in a settled period have their money locked until it's undone.
* People with history are archived, never deleted. Archiving needs their part of the current
  period to be settled first, but undoing a settle-up can bring an archived person's rows back,
  so archived people still join the calculation (at their 0% share) whenever open rows
  mention them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CostCategory,
    Partner,
    PartnerPayment,
    Product,
    Sale,
    Settlement,
    UpfrontCost,
    User,
)
from app.money import even_shares
from app.services import audit
from app.services.errors import (
    CannotSettleError,
    ConflictError,
    LockedError,
    NotFoundError,
    ValidationFailed,
)
from app.services.notifications import Kind, dismiss, notify
from app.services.split_engine import (
    Issue,
    Payment,
    Person,
    SaleMoney,
    SplitResult,
    calculate,
)
from app.services.split_engine import UpfrontCost as EngineCost
from app.services.validation import (
    Errors,
    checked,
    clean_date,
    clean_money,
    clean_percent,
    clean_text,
    violated_constraint,
)

NAME_MAX = 50
DESCRIPTION_MAX = 200
NOTES_MAX = 1000
PAYMENT_NOTE_MAX = 200
_NAME_INDEX = "uq_partners_user_lower_name"
_ZERO_SHARE = Decimal("0.00")
_FULL_SHARE = Decimal("100.00")
_SPLIT_URL = "/app/split"
_LOCKED = "This is part of a settle-up. Undo the settle-up to change its money."
_ISSUE_MESSAGES = {
    Issue.NO_PEOPLE: "Add the people who share the profit first.",
    Issue.SHARES_NOT_100: "Shares need to total exactly 100% before you can settle up.",
    Issue.UNASSIGNED_SALES: "Choose who usually receives sale money before you can settle up.",
}


@dataclass(frozen=True, slots=True)
class PeriodView:
    """The current period: the calculation, and when it started and ended."""

    result: SplitResult
    start: date | None
    end: date | None
    row_count: int


@dataclass(frozen=True, slots=True)
class UndoResult:
    settlement_id: int
    #: The shares were changed after the settle-up, so balances won't match it any more.
    shares_changed: bool


# -- People ---------------------------------------------------------------------------------


def list_people(user_id: int, *, include_archived: bool = False) -> Sequence[Partner]:
    query = select(Partner).where(Partner.user_id == user_id).order_by(Partner.id)
    if not include_archived:
        query = query.where(Partner.archived_at.is_(None))
    return db.session.scalars(query).all()


def get_person(user_id: int, partner_id: int, *, for_update: bool = False) -> Partner:
    query = select(Partner).where(Partner.id == partner_id, Partner.user_id == user_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    person = db.session.scalar(query)
    if person is None:
        raise NotFoundError("That person doesn't exist.")
    return person


def add_person(
    user_id: int,
    *,
    name: str,
    share_pct: Decimal | int | str,
    usually_receives_money: bool = False,
) -> Partner:
    """Add someone to the split. The first person becomes the usual receiver automatically."""
    errors = Errors()
    clean_name = clean_text(
        errors, "name", name, max_length=NAME_MAX, required=True, label="The name"
    )
    share = clean_percent(errors, "share_pct", share_pct)
    errors.raise_if_any()

    _lock_account(user_id)
    is_first = _usual_receiver_id(user_id) is None and not _active_people(user_id)
    if usually_receives_money:
        _clear_usual_receiver(user_id)
    person = Partner(
        user_id=user_id,
        name=checked(clean_name),
        share_pct=checked(share),
        receives_money_default=usually_receives_money or is_first,
    )
    db.session.add(person)
    _flush_names(user_id)
    _warn_if_shares_off(user_id)
    db.session.commit()
    return person


def update_person(
    user_id: int, partner_id: int, *, name: str, share_pct: Decimal | int | str
) -> Partner:
    errors = Errors()
    clean_name = clean_text(
        errors, "name", name, max_length=NAME_MAX, required=True, label="The name"
    )
    share = clean_percent(errors, "share_pct", share_pct)
    errors.raise_if_any()

    _lock_account(user_id)
    person = get_person(user_id, partner_id)
    if person.archived_at is not None:
        db.session.rollback()
        raise ValidationFailed(name="Restore this person before changing them.")
    person.name = checked(clean_name)
    person.share_pct = checked(share)
    _flush_names(user_id)
    _warn_if_shares_off(user_id)
    db.session.commit()
    return person


def set_even_shares(user_id: int) -> Sequence[Partner]:
    """Give everyone an equal share, totalling exactly 100%."""
    _lock_account(user_id)
    people = _active_people(user_id)
    if people:
        for person, share in zip(people, even_shares(len(people)), strict=True):
            person.share_pct = share
        db.session.flush()
        _warn_if_shares_off(user_id)
    db.session.commit()
    return people


def set_usual_receiver(user_id: int, partner_id: int) -> Partner:
    _lock_account(user_id)
    person = get_person(user_id, partner_id)
    if person.archived_at is not None:
        db.session.rollback()
        raise ValidationFailed(partner_id="Restore this person first.")
    _clear_usual_receiver(user_id)
    person.receives_money_default = True
    db.session.commit()
    return person


def archive_person(user_id: int, partner_id: int) -> Partner:
    """Take someone out of the split but keep their history. Their share goes to 0%.

    Blocked while they have anything in the current period: they might be owed money, and
    the shares should be settled as they stood. Settling first keeps everyone's figures right.
    """
    _lock_account(user_id)
    person = get_person(user_id, partner_id)
    if person.archived_at is not None:
        return person

    if _has_open_rows(user_id, person):
        db.session.rollback()
        raise ConflictError(
            partner_id=f"{person.name} has activity since the last settle-up. Settle up first."
        )

    was_usual = person.receives_money_default
    person.archived_at = datetime.now(UTC)
    person.share_pct = _ZERO_SHARE
    person.receives_money_default = False
    db.session.flush()
    if was_usual:
        # Only an obvious successor is picked. With several people left, guessing could
        # credit sale money to the wrong person, so the user is made to choose.
        remaining = _active_people(user_id)
        if len(remaining) == 1:
            remaining[0].receives_money_default = True
    _warn_if_shares_off(user_id)
    db.session.commit()
    return person


def restore_person(user_id: int, partner_id: int) -> Partner:
    """Bring an archived person back at a 0% share, unless their name has been taken since."""
    _lock_account(user_id)
    person = get_person(user_id, partner_id)
    if person.archived_at is None:
        return person
    needs_receiver = _usual_receiver_id(user_id) is None
    person.archived_at = None
    person.receives_money_default = needs_receiver
    _flush_names(user_id)
    db.session.commit()
    return person


def delete_person(user_id: int, partner_id: int) -> None:
    """Remove someone added by mistake. Anyone with history can only be archived."""
    _lock_account(user_id)
    person = get_person(user_id, partner_id)
    if _has_any_history(user_id, partner_id):
        db.session.rollback()
        raise ConflictError(
            partner_id=f"{person.name} has history, so they can only be archived, not deleted."
        )
    was_usual = person.receives_money_default
    db.session.delete(person)
    db.session.flush()
    if was_usual:
        remaining = _active_people(user_id)
        if len(remaining) == 1:
            remaining[0].receives_money_default = True
    _warn_if_shares_off(user_id)
    db.session.commit()


# -- Upfront costs --------------------------------------------------------------------------


def add_cost(
    user_id: int,
    *,
    description: str,
    category: CostCategory | str,
    amount: Decimal | int | str,
    cost_date: date | str | None,
    paid_by_partner_id: int,
    product_id: int | None = None,
    notes: str | None = None,
) -> UpfrontCost:
    errors = Errors()
    clean_description = clean_text(
        errors,
        "description",
        description,
        max_length=DESCRIPTION_MAX,
        required=True,
        label="The description",
    )
    clean_category = _clean_category(errors, category)
    clean_amount = clean_money(errors, "amount", amount, allow_zero=False)
    clean_day = clean_date(errors, "cost_date", cost_date)
    clean_notes = clean_text(errors, "notes", notes, max_length=NOTES_MAX)
    _check_active_person(errors, "paid_by_partner_id", user_id, paid_by_partner_id)
    _check_product(errors, user_id, product_id)
    errors.raise_if_any()

    cost = UpfrontCost(
        user_id=user_id,
        description=checked(clean_description),
        category=checked(clean_category),
        amount=checked(clean_amount),
        cost_date=checked(clean_day),
        paid_by_partner_id=paid_by_partner_id,
        product_id=product_id,
        notes=clean_notes,
    )
    db.session.add(cost)
    db.session.commit()
    return cost


def get_cost(user_id: int, cost_id: int, *, for_update: bool = False) -> UpfrontCost:
    query = select(UpfrontCost).where(UpfrontCost.id == cost_id, UpfrontCost.user_id == user_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    cost = db.session.scalar(query)
    if cost is None:
        raise NotFoundError("That cost doesn't exist.")
    return cost


def update_cost(
    user_id: int,
    cost_id: int,
    *,
    description: str,
    category: CostCategory | str,
    amount: Decimal | int | str,
    cost_date: date | str | None,
    paid_by_partner_id: int,
    product_id: int | None = None,
    notes: str | None = None,
) -> UpfrontCost:
    cost = get_cost(user_id, cost_id, for_update=True)
    errors = Errors()
    clean_description = clean_text(
        errors,
        "description",
        description,
        max_length=DESCRIPTION_MAX,
        required=True,
        label="The description",
    )
    clean_category = _clean_category(errors, category)
    clean_amount = clean_money(errors, "amount", amount, allow_zero=False)
    clean_day = clean_date(errors, "cost_date", cost_date)
    clean_notes = clean_text(errors, "notes", notes, max_length=NOTES_MAX)
    if paid_by_partner_id != cost.paid_by_partner_id:
        _check_active_person(errors, "paid_by_partner_id", user_id, paid_by_partner_id)
    if product_id != cost.product_id:
        _check_product(errors, user_id, product_id)
    errors.raise_if_any()

    money_changed = clean_amount != cost.amount or paid_by_partner_id != cost.paid_by_partner_id
    if cost.settlement_id is not None and money_changed:
        db.session.rollback()
        raise LockedError(_LOCKED)

    cost.description = checked(clean_description)
    cost.category = checked(clean_category)
    cost.amount = checked(clean_amount)
    cost.cost_date = checked(clean_day)
    cost.paid_by_partner_id = paid_by_partner_id
    cost.product_id = product_id
    cost.notes = clean_notes
    db.session.commit()
    return cost


def delete_cost(user_id: int, cost_id: int) -> None:
    cost = get_cost(user_id, cost_id, for_update=True)
    if cost.settlement_id is not None:
        db.session.rollback()
        raise LockedError(_LOCKED)
    db.session.delete(cost)
    db.session.commit()


def list_costs(user_id: int, *, open_only: bool = False) -> Sequence[UpfrontCost]:
    query = select(UpfrontCost).where(UpfrontCost.user_id == user_id)
    if open_only:
        query = query.where(UpfrontCost.settlement_id.is_(None))
    return db.session.scalars(
        query.order_by(UpfrontCost.cost_date.desc(), UpfrontCost.id.desc())
    ).all()


# -- Payments between people ----------------------------------------------------------------


def record_payment(
    user_id: int,
    *,
    from_partner_id: int,
    to_partner_id: int,
    amount: Decimal | int | str,
    paid_on: date | str | None,
    note: str | None = None,
) -> PartnerPayment:
    """Record money one person gave another, e.g. part-paying a debt."""
    errors = Errors()
    clean_amount = clean_money(errors, "amount", amount, allow_zero=False)
    clean_day = clean_date(errors, "paid_on", paid_on)
    clean_note = clean_text(errors, "note", note, max_length=PAYMENT_NOTE_MAX)
    _check_active_person(errors, "from_partner_id", user_id, from_partner_id)
    _check_active_person(errors, "to_partner_id", user_id, to_partner_id)
    if from_partner_id == to_partner_id:
        errors.add("to_partner_id", "A payment has to be between two different people.")
    errors.raise_if_any()

    payment = PartnerPayment(
        user_id=user_id,
        from_partner_id=from_partner_id,
        to_partner_id=to_partner_id,
        amount=checked(clean_amount),
        paid_on=checked(clean_day),
        note=clean_note,
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def get_payment(user_id: int, payment_id: int, *, for_update: bool = False) -> PartnerPayment:
    query = select(PartnerPayment).where(
        PartnerPayment.id == payment_id, PartnerPayment.user_id == user_id
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    payment = db.session.scalar(query)
    if payment is None:
        raise NotFoundError("That payment doesn't exist.")
    return payment


def update_payment(
    user_id: int,
    payment_id: int,
    *,
    from_partner_id: int,
    to_partner_id: int,
    amount: Decimal | int | str,
    paid_on: date | str | None,
    note: str | None = None,
) -> PartnerPayment:
    payment = get_payment(user_id, payment_id, for_update=True)
    errors = Errors()
    clean_amount = clean_money(errors, "amount", amount, allow_zero=False)
    clean_day = clean_date(errors, "paid_on", paid_on)
    clean_note = clean_text(errors, "note", note, max_length=PAYMENT_NOTE_MAX)
    if from_partner_id != payment.from_partner_id:
        _check_active_person(errors, "from_partner_id", user_id, from_partner_id)
    if to_partner_id != payment.to_partner_id:
        _check_active_person(errors, "to_partner_id", user_id, to_partner_id)
    if from_partner_id == to_partner_id:
        errors.add("to_partner_id", "A payment has to be between two different people.")
    errors.raise_if_any()

    money_changed = (
        clean_amount != payment.amount
        or from_partner_id != payment.from_partner_id
        or to_partner_id != payment.to_partner_id
    )
    if payment.settlement_id is not None and money_changed:
        db.session.rollback()
        raise LockedError(_LOCKED)

    payment.from_partner_id = from_partner_id
    payment.to_partner_id = to_partner_id
    payment.amount = checked(clean_amount)
    payment.paid_on = checked(clean_day)
    payment.note = clean_note
    db.session.commit()
    return payment


def delete_payment(user_id: int, payment_id: int) -> None:
    payment = get_payment(user_id, payment_id, for_update=True)
    if payment.settlement_id is not None:
        db.session.rollback()
        raise LockedError(_LOCKED)
    db.session.delete(payment)
    db.session.commit()


def list_payments(user_id: int, *, open_only: bool = False) -> Sequence[PartnerPayment]:
    query = select(PartnerPayment).where(PartnerPayment.user_id == user_id)
    if open_only:
        query = query.where(PartnerPayment.settlement_id.is_(None))
    return db.session.scalars(
        query.order_by(PartnerPayment.paid_on.desc(), PartnerPayment.id.desc())
    ).all()


# -- The current period ---------------------------------------------------------------------


def current_period(user_id: int) -> PeriodView:
    """Who owes whom right now. Totals come from SQL rather than loading every row."""
    sales = [
        SaleMoney(amount=amount, received_by=receiver)
        for receiver, amount in db.session.execute(
            select(Sale.received_by_partner_id, func.sum(Sale.price_paid))
            .where(*_open_sales(user_id))
            .group_by(Sale.received_by_partner_id)
        )
    ]
    costs = [
        EngineCost(amount=amount, paid_by=paid_by)
        for paid_by, amount in db.session.execute(
            select(UpfrontCost.paid_by_partner_id, func.sum(UpfrontCost.amount))
            .where(UpfrontCost.user_id == user_id, UpfrontCost.settlement_id.is_(None))
            .group_by(UpfrontCost.paid_by_partner_id)
        )
    ]
    payments = [
        Payment(amount=amount, from_id=from_id, to_id=to_id)
        for from_id, to_id, amount in db.session.execute(
            select(
                PartnerPayment.from_partner_id,
                PartnerPayment.to_partner_id,
                func.sum(PartnerPayment.amount),
            )
            .where(PartnerPayment.user_id == user_id, PartnerPayment.settlement_id.is_(None))
            .group_by(PartnerPayment.from_partner_id, PartnerPayment.to_partner_id)
        )
    ]
    result = _calculate(user_id, sales, costs, payments)

    sale_start, sale_end, sale_count = db.session.execute(
        select(func.min(Sale.sale_date), func.max(Sale.sale_date), func.count()).where(
            *_open_sales(user_id)
        )
    ).one()
    cost_start, cost_end, cost_count = db.session.execute(
        select(
            func.min(UpfrontCost.cost_date), func.max(UpfrontCost.cost_date), func.count()
        ).where(UpfrontCost.user_id == user_id, UpfrontCost.settlement_id.is_(None))
    ).one()
    payment_start, payment_end, payment_count = db.session.execute(
        select(
            func.min(PartnerPayment.paid_on), func.max(PartnerPayment.paid_on), func.count()
        ).where(PartnerPayment.user_id == user_id, PartnerPayment.settlement_id.is_(None))
    ).one()
    starts = [d for d in (sale_start, cost_start, payment_start) if d is not None]
    ends = [d for d in (sale_end, cost_end, payment_end) if d is not None]
    return PeriodView(
        result=result,
        start=min(starts, default=None),
        end=max(ends, default=None),
        row_count=sale_count + cost_count + payment_count,
    )


def settle_up(user_id: int, *, actor_id: int | None = None) -> Settlement:
    """Close the current period: save a snapshot, stamp the rows and reset the tally to $0.

    Works only from the rows it has locked, so a sale saved a moment later simply lands in
    the next period instead of being stamped without having been counted.
    """
    _lock_account(user_id)
    # Shares can't change underneath the calculation. key_share=True is "FOR NO KEY UPDATE",
    # which still stops updates but not the foreign key checks of a sale being saved. Those
    # wait for nothing here, so that a sale edit holding a row this needs can always finish.
    db.session.scalars(
        select(Partner)
        .where(Partner.user_id == user_id)
        .order_by(Partner.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).all()

    sale_rows = db.session.execute(
        select(Sale.id, Sale.received_by_partner_id, Sale.price_paid, Sale.sale_date)
        .where(*_open_sales(user_id))
        .order_by(Sale.id)
        .with_for_update()
    ).all()
    cost_rows = db.session.execute(
        select(
            UpfrontCost.id,
            UpfrontCost.paid_by_partner_id,
            UpfrontCost.amount,
            UpfrontCost.cost_date,
        )
        .where(UpfrontCost.user_id == user_id, UpfrontCost.settlement_id.is_(None))
        .order_by(UpfrontCost.id)
        .with_for_update()
    ).all()
    payment_rows = db.session.execute(
        select(
            PartnerPayment.id,
            PartnerPayment.from_partner_id,
            PartnerPayment.to_partner_id,
            PartnerPayment.amount,
            PartnerPayment.paid_on,
        )
        .where(PartnerPayment.user_id == user_id, PartnerPayment.settlement_id.is_(None))
        .order_by(PartnerPayment.id)
        .with_for_update()
    ).all()

    if not (sale_rows or cost_rows or payment_rows):
        db.session.rollback()
        raise CannotSettleError("There's nothing to settle yet.")

    result = _calculate(
        user_id,
        [
            SaleMoney(amount=row.price_paid, received_by=row.received_by_partner_id)
            for row in sale_rows
        ],
        [EngineCost(amount=row.amount, paid_by=row.paid_by_partner_id) for row in cost_rows],
        [
            Payment(amount=row.amount, from_id=row.from_partner_id, to_id=row.to_partner_id)
            for row in payment_rows
        ],
    )
    if not result.can_settle:
        db.session.rollback()
        raise CannotSettleError(
            _ISSUE_MESSAGES[result.issues[0]], tuple(issue.value for issue in result.issues)
        )

    dates = [
        *(row.sale_date for row in sale_rows),
        *(row.cost_date for row in cost_rows),
        *(row.paid_on for row in payment_rows),
    ]
    settlement = Settlement(
        user_id=user_id,
        period_start=min(dates),
        period_end=max(dates),
        money_in=result.money_in,
        upfront_total=result.upfront_total,
        profit_to_share=result.profit_to_share,
        snapshot=result.to_snapshot(),
    )
    db.session.add(settlement)
    db.session.flush()

    _stamp(Sale, [row.id for row in sale_rows], settlement.id)
    _stamp(UpfrontCost, [row.id for row in cost_rows], settlement.id)
    _stamp(PartnerPayment, [row.id for row in payment_rows], settlement.id)

    audit.record(
        "split.settled",
        actor_id=actor_id,
        target_type="settlement",
        target_id=settlement.id,
        details={
            "money_in": str(result.money_in),
            "upfront_total": str(result.upfront_total),
            "profit_to_share": str(result.profit_to_share),
        },
    )
    notify(
        user_id,
        Kind.SETTLED,
        "Settle-up recorded",
        "The tally has been reset to $0.00. You can see the breakdown in the split history.",
        url=_SPLIT_URL,
    )
    db.session.commit()
    return settlement


def undo_last_settlement(user_id: int, *, actor_id: int | None = None) -> UndoResult:
    """Put the most recent settle-up's rows back in the current period and forget its snapshot.

    Only the latest can be undone, so periods can't be reopened out of order.
    """
    _lock_account(user_id)
    settlement = db.session.scalar(
        select(Settlement)
        .where(Settlement.user_id == user_id)
        .order_by(Settlement.settled_at.desc(), Settlement.id.desc())
        .limit(1)
        .with_for_update()
    )
    if settlement is None:
        db.session.rollback()
        raise NotFoundError("There's no settle-up to undo.")

    for model in (Sale, UpfrontCost, PartnerPayment):
        db.session.execute(
            update(model)
            .where(model.user_id == user_id, model.settlement_id == settlement.id)
            .values(settlement_id=None)
        )

    settled_shares = {
        person["person_id"]: Decimal(person["share_pct"])
        for person in settlement.snapshot["people"]
        if Decimal(person["share_pct"])
    }
    current_shares = {p.id: p.share_pct for p in _active_people(user_id) if p.share_pct}
    shares_changed = settled_shares != current_shares

    settlement_id = settlement.id
    audit.record(
        "split.settlement_undone",
        actor_id=actor_id,
        target_type="settlement",
        target_id=settlement_id,
        # The settlement row goes away, so this is the only record of what it said.
        details={"snapshot": settlement.snapshot},
    )
    db.session.delete(settlement)
    body = "The rows are back in the current period."
    if shares_changed:
        body += " Shares have changed since, so balances may differ from that settle-up."
    notify(user_id, Kind.SETTLEMENT_UNDONE, "Settle-up undone", body, url=_SPLIT_URL)
    db.session.commit()
    return UndoResult(settlement_id=settlement_id, shares_changed=shares_changed)


def list_settlements(user_id: int, *, limit: int = 50, offset: int = 0) -> Sequence[Settlement]:
    return db.session.scalars(
        select(Settlement)
        .where(Settlement.user_id == user_id)
        .order_by(Settlement.settled_at.desc(), Settlement.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()


def get_settlement(user_id: int, settlement_id: int) -> Settlement:
    settlement = db.session.scalar(
        select(Settlement).where(Settlement.id == settlement_id, Settlement.user_id == user_id)
    )
    if settlement is None:
        raise NotFoundError("That settle-up doesn't exist.")
    return settlement


# -- Helpers --------------------------------------------------------------------------------


def _calculate(
    user_id: int,
    sales: Iterable[SaleMoney],
    costs: Iterable[EngineCost],
    payments: Iterable[Payment],
) -> SplitResult:
    sales, costs, payments = list(sales), list(costs), list(payments)
    referenced = (
        {sale.received_by for sale in sales if sale.received_by is not None}
        | {cost.paid_by for cost in costs}
        | {payment.from_id for payment in payments}
        | {payment.to_id for payment in payments}
    )
    # Everyone active, plus anyone archived who still appears in this period's rows.
    people = db.session.scalars(
        select(Partner)
        .where(
            Partner.user_id == user_id,
            or_(Partner.archived_at.is_(None), Partner.id.in_(referenced)),
        )
        .order_by(Partner.id)
    ).all()
    return calculate(
        [Person(id=p.id, name=p.name, share_pct=p.share_pct) for p in people],
        sales,
        costs,
        payments,
        default_receiver_id=_usual_receiver_id(user_id),
    )


def _open_sales(user_id: int) -> tuple[ColumnElement[bool], ...]:
    return (Sale.user_id == user_id, Sale.settlement_id.is_(None), Sale.deleted_at.is_(None))


def _stamp(
    model: type[Sale] | type[UpfrontCost] | type[PartnerPayment], ids: list[int], settlement_id: int
) -> None:
    if ids:
        db.session.execute(
            update(model).where(model.id.in_(ids)).values(settlement_id=settlement_id)
        )


def _lock_account(user_id: int) -> None:
    """Serialise changes to one account's split, so two at once can't interleave."""
    db.session.execute(select(User.id).where(User.id == user_id).with_for_update())


def _active_people(user_id: int) -> Sequence[Partner]:
    return list_people(user_id)


def _usual_receiver_id(user_id: int) -> int | None:
    return db.session.scalar(
        select(Partner.id).where(
            Partner.user_id == user_id,
            Partner.receives_money_default.is_(True),
            Partner.archived_at.is_(None),
        )
    )


def _clear_usual_receiver(user_id: int) -> None:
    # Flushed straight away: only one usual receiver can exist at any moment.
    db.session.execute(
        update(Partner)
        .where(Partner.user_id == user_id, Partner.receives_money_default.is_(True))
        .values(receives_money_default=False)
        .execution_options(synchronize_session="fetch")
    )


def _has_open_rows(user_id: int, person: Partner) -> bool:
    """Does the current period involve this person, directly or as the usual receiver?"""
    partner_id = person.id
    checks = [
        select(Sale.id).where(*_open_sales(user_id), Sale.received_by_partner_id == partner_id),
        select(UpfrontCost.id).where(
            UpfrontCost.user_id == user_id,
            UpfrontCost.settlement_id.is_(None),
            UpfrontCost.paid_by_partner_id == partner_id,
        ),
        select(PartnerPayment.id).where(
            PartnerPayment.user_id == user_id,
            PartnerPayment.settlement_id.is_(None),
            or_(
                PartnerPayment.from_partner_id == partner_id,
                PartnerPayment.to_partner_id == partner_id,
            ),
        ),
    ]
    if person.receives_money_default:
        # A sale with no receiver is treated as theirs.
        checks.append(
            select(Sale.id).where(*_open_sales(user_id), Sale.received_by_partner_id.is_(None))
        )
    return any(db.session.scalar(check.limit(1)) is not None for check in checks)


def _has_any_history(user_id: int, partner_id: int) -> bool:
    checks = [
        select(Sale.id).where(Sale.user_id == user_id, Sale.received_by_partner_id == partner_id),
        select(UpfrontCost.id).where(
            UpfrontCost.user_id == user_id, UpfrontCost.paid_by_partner_id == partner_id
        ),
        select(PartnerPayment.id).where(
            PartnerPayment.user_id == user_id,
            or_(
                PartnerPayment.from_partner_id == partner_id,
                PartnerPayment.to_partner_id == partner_id,
            ),
        ),
        select(Settlement.id).where(
            Settlement.user_id == user_id,
            Settlement.snapshot.contains({"people": [{"person_id": partner_id}]}),
        ),
    ]
    return any(db.session.scalar(check.limit(1)) is not None for check in checks)


def _flush_names(user_id: int) -> None:
    """Save now, turning a clash on the unique name index into a friendly error."""
    try:
        db.session.flush()
    except IntegrityError as error:
        db.session.rollback()
        if violated_constraint(error) == _NAME_INDEX:
            raise ConflictError(name="You already have a person with that name.") from None
        raise


def _warn_if_shares_off(user_id: int) -> None:
    """Warn when shares don't total 100%, and clear the warning once they do."""
    count, total = db.session.execute(
        select(func.count(), func.coalesce(func.sum(Partner.share_pct), 0)).where(
            Partner.user_id == user_id, Partner.archived_at.is_(None)
        )
    ).one()
    if count and total != _FULL_SHARE:
        notify(
            user_id,
            Kind.SHARES_NOT_100,
            "Shares don't total 100%",
            "You can't settle up until they do.",
            url=_SPLIT_URL,
            unless_unread=True,
        )
    else:
        dismiss(user_id, Kind.SHARES_NOT_100)


def _clean_category(errors: Errors, category: CostCategory | str) -> CostCategory | None:
    try:
        return CostCategory(category)
    except ValueError:
        errors.add("category", "Choose a category.")
        return None


def _check_active_person(errors: Errors, field: str, user_id: int, partner_id: int) -> None:
    found = db.session.scalar(
        select(Partner.id).where(
            Partner.id == partner_id, Partner.user_id == user_id, Partner.archived_at.is_(None)
        )
    )
    if found is None:
        errors.add(field, "Choose one of your people.")


def _check_product(errors: Errors, user_id: int, product_id: int | None) -> None:
    if product_id is None:
        return
    found = db.session.scalar(
        select(Product.id).where(Product.id == product_id, Product.user_id == user_id)
    )
    if found is None:
        errors.add("product_id", "Choose one of your products.")
