"""Things that only go wrong when two requests happen at once.

Each thread gets its own app context and so its own database connection, as real requests do.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import Product, RecoveryCode, Sale, Settlement, User
from app.services import auth, products, sales, split
from app.services.errors import (
    AuthenticationError,
    CannotSettleError,
    ConflictError,
    LockedError,
    NotFoundError,
)
from tests.factories import PASSWORD, UserFactory

pytestmark = pytest.mark.integration

DAY = date(2026, 11, 5)


@pytest.fixture
def account(app, ctx):
    user = UserFactory()
    db.session.commit()
    alex = split.add_person(user.id, name="Alex", share_pct="50")
    sam = split.add_person(user.id, name="Sam", share_pct="50")
    product = products.create_product(user.id, name="Stamp", cost="1")
    return {"user_id": user.id, "alex": alex.id, "sam": sam.id, "product": product.id}


def run_together(app, *tasks):
    """Run each task at the same moment, in its own context. Returns result or exception."""
    barrier = threading.Barrier(len(tasks))

    def runner(task):
        with app.app_context():
            barrier.wait()
            try:
                return task()
            except Exception as error:
                db.session.rollback()
                return error
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return [f.result() for f in [pool.submit(runner, t) for t in tasks]]


def test_two_requests_cannot_create_duplicate_product_names(app, account):
    """Audit issue 8: this was only checked in Python, so two requests at once both won."""
    user_id = account["user_id"]

    for attempt in range(5):
        results = run_together(
            app,
            *[
                lambda name=f"Same {attempt}": products.create_product(user_id, name=name, cost="1")
                for _ in range(4)
            ],
        )
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        created = [r for r in results if not isinstance(r, Exception)]
        assert len(created) == 1
        assert len(conflicts) == 3

    db.session.rollback()
    count = db.session.scalar(select(func.count()).select_from(Product))
    assert count == 6, "the fixture's product plus one of each attempt"


def test_two_people_cannot_register_the_same_username(app, ctx):
    results = run_together(app, *[lambda: auth.register("alex", PASSWORD) for _ in range(4)])
    assert sum(isinstance(r, ConflictError) for r in results) == 3
    db.session.rollback()
    assert db.session.scalar(select(func.count()).select_from(User)) == 1


def test_a_recovery_code_cannot_be_spent_twice_at_once(app, ctx):
    code = auth.register("alex", PASSWORD).recovery_codes[0]
    results = run_together(
        app,
        *[
            lambda number=number: auth.recover_account(
                "alex", code, f"a-brand-new-passphrase-{number}"
            )
            for number in range(4)
        ],
    )
    assert sum(isinstance(r, AuthenticationError) for r in results) == 3
    db.session.rollback()
    used = db.session.scalar(
        select(func.count()).select_from(RecoveryCode).where(RecoveryCode.used_at.is_not(None))
    )
    assert used == 1


def test_settling_twice_at_once_makes_one_settlement(app, account):
    user_id = account["user_id"]
    sales.create_sale(
        user_id,
        product_id=account["product"],
        sale_date=DAY,
        quantity=1,
        price_paid="10",
        received_by_partner_id=account["sam"],
    )
    results = run_together(app, *[lambda: split.settle_up(user_id) for _ in range(4)])
    assert sum(isinstance(r, CannotSettleError) for r in results) == 3
    db.session.rollback()
    assert db.session.scalar(select(func.count()).select_from(Settlement)) == 1


def test_undoing_twice_at_once_reopens_once(app, account):
    user_id = account["user_id"]
    sales.create_sale(
        user_id,
        product_id=account["product"],
        sale_date=DAY,
        quantity=1,
        price_paid="10",
        received_by_partner_id=account["sam"],
    )
    split.settle_up(user_id)
    results = run_together(app, *[lambda: split.undo_last_settlement(user_id) for _ in range(3)])
    assert sum(isinstance(r, NotFoundError) for r in results) == 2
    db.session.rollback()
    assert split.current_period(user_id).row_count == 1


def test_sales_saved_during_a_settle_up_are_never_lost(app, account):
    """Whatever the timing, every sale is either in the settlement's figures or still open."""
    user_id = account["user_id"]
    sales.create_sale(
        user_id,
        product_id=account["product"],
        sale_date=DAY,
        quantity=1,
        price_paid="100.00",
        received_by_partner_id=account["sam"],
    )

    def add_sale():
        return sales.create_sale(
            user_id,
            product_id=account["product"],
            sale_date=DAY,
            quantity=1,
            price_paid="1.00",
            received_by_partner_id=account["sam"],
        )

    for _ in range(10):
        run_together(app, add_sale, add_sale, lambda: split.settle_up(user_id), add_sale)
        db.session.rollback()
        total_sold = db.session.scalar(select(func.sum(Sale.price_paid)))
        settled = db.session.scalar(select(func.coalesce(func.sum(Settlement.money_in), 0)))
        still_open = split.current_period(user_id).result.money_in
        assert settled + still_open == total_sold

        stamped = db.session.execute(
            select(Sale.settlement_id, func.sum(Sale.price_paid))
            .where(Sale.settlement_id.is_not(None))
            .group_by(Sale.settlement_id)
        ).all()
        for settlement_id, amount in stamped:
            assert db.session.get(Settlement, settlement_id).money_in == amount
        db.session.rollback()


def test_an_edit_racing_a_settle_up_is_either_counted_or_refused(app, account):
    user_id = account["user_id"]
    for attempt in range(10):
        sale = sales.create_sale(
            user_id,
            product_id=account["product"],
            sale_date=DAY,
            quantity=1,
            price_paid="10.00",
            received_by_partner_id=account["sam"],
        )
        sale_id = sale.id

        def edit(sale_id=sale_id):
            return sales.update_sale(
                user_id,
                sale_id,
                sale_date=DAY,
                quantity=1,
                price_paid="99.00",
                received_by_partner_id=account["sam"],
            )

        edited, settlement_id = run_together(app, edit, lambda: split.settle_up(user_id).id)
        assert not isinstance(settlement_id, Exception), attempt
        db.session.rollback()
        db.session.expire_all()
        price = db.session.get(Sale, sale_id).price_paid
        recorded = db.session.get(Settlement, settlement_id).money_in
        if isinstance(edited, LockedError):
            assert price == recorded == Decimal("10.00")
        else:
            assert price == recorded == Decimal("99.00")
        db.session.rollback()


def test_changing_who_received_a_sale_cannot_deadlock_with_a_settle_up(app, account):
    """Changing the receiver re-checks a foreign key at commit, while settling holds both rows."""
    user_id = account["user_id"]
    for _ in range(15):
        sale_id = sales.create_sale(
            user_id,
            product_id=account["product"],
            sale_date=DAY,
            quantity=1,
            price_paid="10.00",
            received_by_partner_id=account["sam"],
        ).id

        def edit(sale_id=sale_id):
            return sales.update_sale(
                user_id,
                sale_id,
                sale_date=DAY,
                quantity=1,
                price_paid="10.00",
                received_by_partner_id=account["alex"],
            ).id

        edited, settled = run_together(app, edit, lambda: split.settle_up(user_id).id)
        assert isinstance(settled, int), repr(settled)
        assert isinstance(edited, int | LockedError), repr(edited)
        db.session.rollback()


def test_settling_and_bulk_deleting_at_once_cannot_deadlock(app, account):
    user_id = account["user_id"]
    for _ in range(5):
        ids = [
            sales.create_sale(
                user_id,
                product_id=account["product"],
                sale_date=DAY,
                quantity=1,
                price_paid="1.00",
                received_by_partner_id=account["sam"],
            ).id
            for _ in range(30)
        ]
        results = run_together(
            app,
            lambda ids=ids: sales.delete_sales(user_id, list(reversed(ids))).deleted_ids,
            lambda: split.settle_up(user_id).id,
        )
        assert not [
            r for r in results if isinstance(r, Exception) and not isinstance(r, CannotSettleError)
        ], results
        db.session.rollback()


def test_changing_the_usual_receiver_at_once_leaves_exactly_one(app, account):
    user_id = account["user_id"]
    for _ in range(5):
        results = run_together(
            app,
            lambda: split.set_usual_receiver(user_id, account["alex"]),
            lambda: split.set_usual_receiver(user_id, account["sam"]),
        )
        assert not [r for r in results if isinstance(r, Exception)]
        db.session.rollback()
        db.session.expire_all()
        receivers = [p for p in split.list_people(user_id) if p.receives_money_default]
        assert len(receivers) == 1
        db.session.rollback()
