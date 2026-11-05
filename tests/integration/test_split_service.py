"""The split on real data: people, costs, payments, settling up and undoing it."""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    AuditLog,
    CostCategory,
    Notification,
    Partner,
    PartnerPayment,
    Sale,
    Settlement,
    UpfrontCost,
)
from app.services import notifications, products, sales, split
from app.services.errors import (
    CannotSettleError,
    ConflictError,
    LockedError,
    NotFoundError,
    ValidationFailed,
)
from app.services.notifications import Kind
from app.services.split_engine import Issue
from tests.factories import SaleFactory, UserFactory
from tests.helpers import count_queries

pytestmark = pytest.mark.integration

DAY = date(2026, 11, 5)


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


@pytest.fixture
def product(user_id):
    return products.create_product(user_id, name="Stamp", cost="2.00")


@pytest.fixture
def alex(user_id):
    return split.add_person(user_id, name="Alex", share_pct="50")


@pytest.fixture
def sam(user_id, alex):
    return split.add_person(user_id, name="Sam", share_pct="50")


def sell(user_id, product, price, received_by=None, day=DAY):
    return sales.create_sale(
        user_id,
        product_id=product.id,
        sale_date=day,
        quantity=1,
        price_paid=price,
        received_by_partner_id=received_by.id if received_by else None,
    )


def spend(user_id, person, amount, day=DAY, **values):
    return split.add_cost(
        user_id,
        description=values.pop("description", "Stock"),
        category=values.pop("category", "stock"),
        amount=amount,
        cost_date=day,
        paid_by_partner_id=person.id,
        **values,
    )


def pay(user_id, sender, receiver, amount, day=DAY):
    return split.record_payment(
        user_id,
        from_partner_id=sender.id,
        to_partner_id=receiver.id,
        amount=amount,
        paid_on=day,
    )


def worked_example(user_id, product, alex, sam):
    """Plan section 3.3."""
    spend(user_id, alex, "35.00")
    spend(user_id, sam, "10.00", category="fees", description="Stall fee")
    sell(user_id, product, "80.00", received_by=sam)
    sell(user_id, product, "40.00", received_by=alex)


def balances(user_id):
    return {p.name: p.balance for p in split.current_period(user_id).result.people}


def shares(user_id):
    return {p.name: p.share_pct for p in split.list_people(user_id)}


class TestWorkedExample:
    def test_sam_pays_alex_32_50(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        view = split.current_period(user_id)
        result = view.result

        assert result.money_in == Decimal("120.00")
        assert result.upfront_total == Decimal("45.00")
        assert result.profit_to_share == Decimal("75.00")

        a, s = result.person(alex.id), result.person(sam.id)
        assert (a.gets_back, s.gets_back) == (Decimal("35.00"), Decimal("10.00"))
        assert (a.share_of_profit, s.share_of_profit) == (Decimal("37.50"), Decimal("37.50"))
        assert (a.should_end_with, s.should_end_with) == (Decimal("72.50"), Decimal("47.50"))
        assert (a.holding, s.holding) == (Decimal("40.00"), Decimal("80.00"))
        assert (a.balance, s.balance) == (Decimal("32.50"), Decimal("-32.50"))

        assert [(t.from_name, t.to_name, t.amount) for t in result.transfers] == [
            ("Sam", "Alex", Decimal("32.50"))
        ]
        assert result.can_settle
        assert view.row_count == 4

    def test_settle_up_resets_the_tally(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        settlement = split.settle_up(user_id)

        assert settlement.money_in == Decimal("120.00")
        assert settlement.upfront_total == Decimal("45.00")
        assert settlement.profit_to_share == Decimal("75.00")
        assert settlement.snapshot["transfers"][0]["amount"] == "32.50"

        view = split.current_period(user_id)
        assert view.result.money_in == Decimal("0.00")
        assert view.result.upfront_total == Decimal("0.00")
        assert view.result.transfers == ()
        assert view.row_count == 0
        assert balances(user_id) == {"Alex": Decimal("0.00"), "Sam": Decimal("0.00")}


class TestRules:
    def test_per_item_product_costs_are_not_used(self, user_id, alex, sam):
        """Split basis A (plan section 3.4): only real money counts."""
        pricey = products.create_product(user_id, name="Pricey", cost="1000.00")
        sell(user_id, pricey, "100.00", received_by=sam)
        result = split.current_period(user_id).result
        assert result.profit_to_share == Decimal("100.00")
        assert result.person(alex.id).balance == Decimal("50.00")

    def test_losses_are_shared(self, user_id, product, alex, sam):
        spend(user_id, alex, "100.00")
        sell(user_id, product, "40.00", received_by=alex)
        result = split.current_period(user_id).result
        assert result.profit_to_share == Decimal("-60.00")
        assert result.person(alex.id).balance == Decimal("30.00")
        assert result.person(sam.id).balance == Decimal("-30.00")
        assert [(t.from_name, t.amount) for t in result.transfers] == [("Sam", Decimal("30.00"))]

    def test_rounding_never_loses_a_cent(self, user_id, product):
        people = [
            split.add_person(user_id, name=name, share_pct=share)
            for name, share in (("A", "33.34"), ("B", "33.33"), ("C", "33.33"))
        ]
        sell(user_id, product, "100.00", received_by=people[0])
        result = split.current_period(user_id).result
        assert sum(p.share_of_profit for p in result.people) == Decimal("100.00")
        assert sum(p.balance for p in result.people) == Decimal("0.00")

    def test_part_payment_reduces_the_debt(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        pay(user_id, sam, alex, "20.00")
        result = split.current_period(user_id).result
        assert result.person(alex.id).balance == Decimal("12.50")
        assert result.person(sam.id).balance == Decimal("-12.50")
        assert result.money_in == Decimal("120.00"), "a payment isn't income"
        assert result.profit_to_share == Decimal("75.00")

    def test_paying_it_all_leaves_nothing_owed(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        pay(user_id, sam, alex, "32.50")
        result = split.current_period(user_id).result
        assert not result.has_debts
        assert result.transfers == ()

    def test_overpaying_flips_the_debt(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        pay(user_id, sam, alex, "50.00")
        assert split.current_period(user_id).result.transfers[0].from_name == "Alex"

    def test_deleted_sales_do_not_count_until_restored(self, user_id, product, alex, sam):
        sale = sell(user_id, product, "100.00", received_by=sam)
        sales.delete_sale(user_id, sale.id)
        assert split.current_period(user_id).result.money_in == Decimal("0.00")
        sales.restore_sales(user_id, [sale.id])
        assert split.current_period(user_id).result.money_in == Decimal("100.00")

    def test_sales_recorded_before_any_people_go_to_the_first_person(self, user_id, product):
        sell(user_id, product, "100.00")
        alex = split.add_person(user_id, name="Alex", share_pct="100")
        result = split.current_period(user_id).result
        assert result.person(alex.id).holding == Decimal("100.00")
        assert result.can_settle

    def test_totals_come_from_sql_not_from_loading_every_row(self, user_id, alex, sam):
        for _ in range(40):
            SaleFactory(user_id=user_id, received_by_partner_id=sam.id, price_paid=Decimal("1.00"))
        db.session.commit()
        with count_queries() as queries:
            view = split.current_period(user_id)
        assert view.result.money_in == Decimal("40.00")
        assert len(queries) <= 8

    def test_random_periods_always_balance_and_settle_to_zero(self, user_id, product):
        """The same invariant the engine's tests check, through the database."""
        rng = random.Random(20261105)
        people = [
            split.add_person(user_id, name=name, share_pct=share)
            for name, share in (("A", "33.34"), ("B", "33.33"), ("C", "33.33"))
        ]
        for _round in range(12):
            for _ in range(rng.randint(1, 6)):
                choice = rng.random()
                amount = f"{rng.randint(1, 50_000) / 100:.2f}"
                who = rng.choice(people)
                if choice < 0.5:
                    sell(user_id, product, amount, received_by=who)
                elif choice < 0.8:
                    spend(user_id, who, amount)
                else:
                    other = rng.choice([p for p in people if p is not who])
                    pay(user_id, who, other, amount)
            result = split.current_period(user_id).result
            assert sum(p.balance for p in result.people) == Decimal("0.00")
            assert result.money_in - result.upfront_total == result.profit_to_share
            total_transfers = {p.person_id: p.balance for p in result.people}
            for transfer in result.transfers:
                total_transfers[transfer.from_id] += transfer.amount
                total_transfers[transfer.to_id] -= transfer.amount
            assert set(total_transfers.values()) <= {Decimal("0.00")}
            split.settle_up(user_id)
            assert not split.current_period(user_id).result.has_debts


class TestSettleUpRules:
    def test_nothing_to_settle(self, user_id, alex, sam):
        with pytest.raises(CannotSettleError, match="nothing to settle"):
            split.settle_up(user_id)

    def test_only_deleted_sales_is_nothing_to_settle(self, user_id, product, alex, sam):
        sales.delete_sale(user_id, sell(user_id, product, "10").id)
        with pytest.raises(CannotSettleError, match="nothing to settle"):
            split.settle_up(user_id)

    def test_shares_must_total_100(self, user_id, product):
        alex = split.add_person(user_id, name="Alex", share_pct="60")
        split.add_person(user_id, name="Sam", share_pct="30")
        sell(user_id, product, "10", received_by=alex)
        result = split.current_period(user_id).result
        assert result.issues == (Issue.SHARES_NOT_100,)
        assert result.shares_total == Decimal("90.00")
        with pytest.raises(CannotSettleError, match="100%") as error:
            split.settle_up(user_id)
        assert error.value.issues == ("shares_not_100",)
        assert db.session.scalar(select(func.count()).select_from(Settlement)) == 0
        assert split.current_period(user_id).row_count == 1, "nothing was stamped"

    def test_no_people(self, user_id, product):
        sell(user_id, product, "10")
        with pytest.raises(CannotSettleError, match="Add the people"):
            split.settle_up(user_id)

    def test_money_with_no_receiver(self, user_id, product, alex, sam):
        SaleFactory(user_id=user_id, price_paid=Decimal("10.00"), received_by_partner_id=None)
        db.session.commit()
        # Remove the usual receiver: now nobody can be assumed to hold that sale's money.
        split._clear_usual_receiver(user_id)
        db.session.commit()
        result = split.current_period(user_id).result
        assert Issue.UNASSIGNED_SALES in result.issues
        assert result.unassigned_money_in == Decimal("10.00")
        with pytest.raises(CannotSettleError, match="usually receives"):
            split.settle_up(user_id)

    def test_a_sale_of_zero_with_no_receiver_is_no_problem(self, user_id, product, alex, sam):
        SaleFactory(user_id=user_id, price_paid=Decimal("0.00"), received_by_partner_id=None)
        db.session.commit()
        split._clear_usual_receiver(user_id)
        db.session.commit()
        assert split.current_period(user_id).result.can_settle

    def test_settling_with_nothing_owed_still_closes_the_period(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=alex)
        pay(user_id, alex, sam, "50")
        assert not split.current_period(user_id).result.has_debts
        settlement = split.settle_up(user_id)
        assert settlement.snapshot["transfers"] == []
        assert split.current_period(user_id).row_count == 0

    def test_snapshot_keeps_the_figures_and_period_dates(self, user_id, product, alex, sam):
        spend(user_id, alex, "35.00", day=date(2026, 10, 3))
        sell(user_id, product, "120.00", received_by=sam, day=date(2026, 11, 2))
        pay(user_id, sam, alex, "1.00", day=date(2026, 11, 9))
        settlement = split.settle_up(user_id)
        assert settlement.period_start == date(2026, 10, 3)
        assert settlement.period_end == date(2026, 11, 9)
        snapshot = settlement.snapshot
        assert snapshot["version"] == 1
        people = {p["name"]: p for p in snapshot["people"]}
        assert people["Alex"]["gets_back"] == "35.00"
        assert people["Sam"]["holding"] == "119.00"
        assert people["Alex"]["share_pct"] == "50.00"

    def test_snapshot_survives_later_changes(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        settlement = split.settle_up(user_id)
        split.update_person(user_id, alex.id, name="Alexandra", share_pct="10")
        split.update_person(user_id, sam.id, name="Samuel", share_pct="90")
        history = split.get_settlement(user_id, settlement.id)
        assert {p["name"] for p in history.snapshot["people"]} == {"Alex", "Sam"}

    def test_later_rows_start_a_new_period(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        first = split.settle_up(user_id)
        sell(user_id, product, "10", received_by=sam)
        result = split.current_period(user_id).result
        assert result.money_in == Decimal("10.00")
        second = split.settle_up(user_id)
        assert second.money_in == Decimal("10.00")
        assert [s.id for s in split.list_settlements(user_id)] == [second.id, first.id]

    def test_stamps_only_this_accounts_rows(self, user_id, product, alex, sam):
        other = UserFactory()
        db.session.commit()
        theirs = SaleFactory(user_id=other.id)
        db.session.commit()
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)
        db.session.refresh(theirs)
        assert theirs.settlement_id is None

    def test_a_soft_deleted_sale_stays_out_and_returns_to_the_new_period(
        self, user_id, product, alex, sam
    ):
        gone = sell(user_id, product, "10", received_by=sam)
        sell(user_id, product, "20", received_by=sam)
        sales.delete_sale(user_id, gone.id)
        settlement = split.settle_up(user_id)
        assert settlement.money_in == Decimal("20.00")

        sales.restore_sales(user_id, [gone.id])
        assert split.current_period(user_id).result.money_in == Decimal("10.00")

    def test_audited_and_notified(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        settlement = split.settle_up(user_id, actor_id=user_id)
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "split.settled"))
        assert entry.actor_id == user_id
        assert entry.target_id == settlement.id
        assert entry.details["money_in"] == "10.00"
        kinds = [n.kind for n in notifications.list_for(user_id)]
        assert Kind.SETTLED.value in kinds


class TestLocks:
    @pytest.fixture
    def settled(self, user_id, product, alex, sam):
        cost = spend(user_id, alex, "10.00")
        payment = pay(user_id, sam, alex, "5.00")
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        return cost, payment

    def _cost_values(self, cost, **changes):
        return {
            "description": cost.description,
            "category": cost.category,
            "amount": cost.amount,
            "cost_date": cost.cost_date,
            "paid_by_partner_id": cost.paid_by_partner_id,
            "product_id": cost.product_id,
            "notes": cost.notes,
            **changes,
        }

    def _payment_values(self, payment, **changes):
        return {
            "from_partner_id": payment.from_partner_id,
            "to_partner_id": payment.to_partner_id,
            "amount": payment.amount,
            "paid_on": payment.paid_on,
            "note": payment.note,
            **changes,
        }

    def test_cost_money_is_locked(self, user_id, sam, settled):
        cost, _ = settled
        with pytest.raises(LockedError):
            split.update_cost(user_id, cost.id, **self._cost_values(cost, amount="11.00"))
        with pytest.raises(LockedError):
            split.update_cost(
                user_id, cost.id, **self._cost_values(cost, paid_by_partner_id=sam.id)
            )
        with pytest.raises(LockedError):
            split.delete_cost(user_id, cost.id)

    def test_cost_details_can_change(self, user_id, settled):
        cost, _ = settled
        updated = split.update_cost(
            user_id, cost.id, **self._cost_values(cost, description="Better name", notes="Hi")
        )
        assert updated.description == "Better name"
        assert updated.notes == "Hi"

    def test_payment_money_is_locked(self, user_id, alex, settled):
        _, payment = settled
        with pytest.raises(LockedError):
            split.update_payment(
                user_id, payment.id, **self._payment_values(payment, amount="6.00")
            )
        with pytest.raises(LockedError):
            split.delete_payment(user_id, payment.id)

    def test_payment_note_can_change(self, user_id, settled):
        _, payment = settled
        updated = split.update_payment(
            user_id, payment.id, **self._payment_values(payment, note="Thanks")
        )
        assert updated.note == "Thanks"

    def test_undo_unlocks_them(self, user_id, settled):
        cost, payment = settled
        split.undo_last_settlement(user_id)
        assert split.update_cost(
            user_id, cost.id, **self._cost_values(cost, amount="11.00")
        ).amount == Decimal("11.00")
        split.delete_payment(user_id, payment.id)


class TestUndo:
    def test_restores_the_period_exactly(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        before = split.current_period(user_id).result
        settlement = split.settle_up(user_id)

        result = split.undo_last_settlement(user_id)

        assert result.settlement_id == settlement.id
        assert result.shares_restored is False
        assert result.people_brought_back == ()
        assert result.people_set_to_zero == ()
        assert split.current_period(user_id).result == before
        assert db.session.scalar(select(func.count()).select_from(Settlement)) == 0

    def test_settling_again_gives_the_same_answer(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        first = split.settle_up(user_id).snapshot
        split.undo_last_settlement(user_id)
        assert split.settle_up(user_id).snapshot == first

    def test_only_the_latest_can_be_undone_one_at_a_time(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        first = split.settle_up(user_id)
        sell(user_id, product, "20", received_by=sam)
        second = split.settle_up(user_id)

        assert split.undo_last_settlement(user_id).settlement_id == second.id
        assert split.current_period(user_id).result.money_in == Decimal("20.00")
        assert split.undo_last_settlement(user_id).settlement_id == first.id
        assert split.current_period(user_id).result.money_in == Decimal("30.00")
        with pytest.raises(NotFoundError):
            split.undo_last_settlement(user_id)

    def test_new_rows_since_then_stay_in_the_period(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)
        sell(user_id, product, "5", received_by=sam)
        split.undo_last_settlement(user_id)
        assert split.current_period(user_id).result.money_in == Decimal("15.00")

    def test_puts_the_old_shares_back(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        split.update_person(user_id, alex.id, name="Alex", share_pct="60")
        split.update_person(user_id, sam.id, name="Sam", share_pct="40")

        result = split.undo_last_settlement(user_id)

        assert result.shares_restored is True
        assert shares(user_id) == {"Alex": Decimal("50.00"), "Sam": Decimal("50.00")}
        assert split.current_period(user_id).result.person(alex.id).balance == Decimal("50.00")
        note = db.session.scalar(
            select(Notification).where(Notification.kind == Kind.SETTLEMENT_UNDONE.value)
        )
        assert "Shares are back to what they were" in note.body

    def test_settling_again_after_shares_changed_gives_the_same_answer(
        self, user_id, product, alex, sam
    ):
        worked_example(user_id, product, alex, sam)
        first = split.settle_up(user_id).snapshot
        split.update_person(user_id, alex.id, name="Alex", share_pct="90")
        split.update_person(user_id, sam.id, name="Sam", share_pct="10")
        split.undo_last_settlement(user_id)
        assert split.settle_up(user_id).snapshot == first

    def test_says_nothing_about_shares_when_they_did_not_change(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        split.undo_last_settlement(user_id)
        note = db.session.scalar(
            select(Notification).where(Notification.kind == Kind.SETTLEMENT_UNDONE.value)
        )
        assert note.body == "The rows are back in the current period."

    def test_each_undo_puts_back_that_settle_ups_shares(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)  # 50/50
        split.update_person(user_id, alex.id, name="Alex", share_pct="60")
        split.update_person(user_id, sam.id, name="Sam", share_pct="40")
        sell(user_id, product, "20", received_by=sam)
        split.settle_up(user_id)  # 60/40
        split.update_person(user_id, alex.id, name="Alex", share_pct="70")
        split.update_person(user_id, sam.id, name="Sam", share_pct="30")

        split.undo_last_settlement(user_id)
        assert shares(user_id) == {"Alex": Decimal("60.00"), "Sam": Decimal("40.00")}
        split.undo_last_settlement(user_id)
        assert shares(user_id) == {"Alex": Decimal("50.00"), "Sam": Decimal("50.00")}

    def test_someone_added_since_goes_to_zero_but_stays(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        third = split.add_person(user_id, name="Third", share_pct="20")
        split.update_person(user_id, alex.id, name="Alex", share_pct="40")
        split.update_person(user_id, sam.id, name="Sam", share_pct="40")
        cost = spend(user_id, third, "10.00")

        result = split.undo_last_settlement(user_id)

        assert result.people_set_to_zero == ("Third",)
        assert shares(user_id) == {
            "Alex": Decimal("50.00"),
            "Sam": Decimal("50.00"),
            "Third": Decimal("0.00"),
        }
        current = split.current_period(user_id).result
        assert current.can_settle
        assert current.person(third.id).gets_back == cost.amount, "their rows still count"
        assert current.person(third.id).share_of_profit == Decimal("0.00")
        note = db.session.scalar(
            select(Notification).where(Notification.kind == Kind.SETTLEMENT_UNDONE.value)
        )
        assert "Third had no share then" in note.body

    def test_someone_archived_since_comes_back_with_their_share(self, user_id, product, alex, sam):
        spend(user_id, sam, "30.00")
        sell(user_id, product, "100", received_by=alex)
        before = split.current_period(user_id).result
        split.settle_up(user_id)
        split.archive_person(user_id, sam.id)
        split.update_person(user_id, alex.id, name="Alex", share_pct="100")

        result = split.undo_last_settlement(user_id)

        assert result.shares_restored is True
        assert result.people_brought_back == ("Sam",)
        assert split.get_person(user_id, sam.id).archived_at is None
        assert shares(user_id) == {"Alex": Decimal("50.00"), "Sam": Decimal("50.00")}
        assert split.current_period(user_id).result == before
        note = db.session.scalar(
            select(Notification).where(Notification.kind == Kind.SETTLEMENT_UNDONE.value)
        )
        assert "Sam had a share then" in note.body

    def test_someone_with_no_share_stays_archived_but_still_counts(
        self, user_id, product, alex, sam
    ):
        """They were at 0%, so restoring shares doesn't need them back. Their rows still count."""
        third = split.add_person(user_id, name="Third", share_pct="0")
        spend(user_id, third, "30.00")
        sell(user_id, product, "100", received_by=alex)
        split.settle_up(user_id)
        split.archive_person(user_id, third.id)

        result = split.undo_last_settlement(user_id)

        assert result.people_brought_back == ()
        assert split.get_person(user_id, third.id).archived_at is not None
        assert [p.name for p in split.list_people(user_id)] == ["Alex", "Sam"]
        current = split.current_period(user_id).result
        assert current.person(third.id).share_pct == Decimal("0.00")
        assert current.person(third.id).gets_back == Decimal("30.00")
        assert sum(p.balance for p in current.people) == Decimal("0.00")
        assert current.can_settle

    def test_a_name_clash_blocks_it_and_changes_nothing(self, user_id, product, alex, sam):
        sale = sell(user_id, product, "100", received_by=alex)
        settlement = split.settle_up(user_id)
        split.archive_person(user_id, sam.id)
        split.update_person(user_id, alex.id, name="Alex", share_pct="60")
        new_sam = split.add_person(user_id, name="SAM", share_pct="40")

        with pytest.raises(ConflictError, match="Sam had a share in this settle-up"):
            split.undo_last_settlement(user_id)

        assert split.get_settlement(user_id, settlement.id).id == settlement.id
        assert split.get_person(user_id, sam.id).archived_at is not None
        assert shares(user_id) == {"Alex": Decimal("60.00"), "SAM": Decimal("40.00")}
        db.session.refresh(sale)
        assert sale.settlement_id == settlement.id

        split.update_person(user_id, new_sam.id, name="Samantha", share_pct="40")
        assert split.undo_last_settlement(user_id).people_brought_back == ("Sam",)

    def test_clears_the_warning_about_shares(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        split.update_person(user_id, alex.id, name="Alex", share_pct="10")
        assert [n.kind for n in notifications.list_for(user_id, unread_only=True)].count(
            Kind.SHARES_NOT_100.value
        ) == 1
        split.undo_last_settlement(user_id)
        assert Kind.SHARES_NOT_100.value not in [
            n.kind for n in notifications.list_for(user_id, unread_only=True)
        ]

    def test_leaves_the_usual_receiver_alone(self, user_id, product, alex, sam):
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        split.set_usual_receiver(user_id, sam.id)
        split.undo_last_settlement(user_id)
        db.session.refresh(sam)
        assert sam.receives_money_default is True

    def test_someone_brought_back_alone_becomes_the_usual_receiver(self, user_id, product):
        alex = split.add_person(user_id, name="Alex", share_pct="100")
        sell(user_id, product, "100", received_by=alex)
        split.settle_up(user_id)
        split.archive_person(user_id, alex.id)
        assert split.undo_last_settlement(user_id).people_brought_back == ("Alex",)
        assert split.get_person(user_id, alex.id).receives_money_default is True

    def test_no_guessing_the_usual_receiver_between_several(self, user_id, product, alex, sam):
        third = split.add_person(user_id, name="Third", share_pct="0")
        sell(user_id, product, "100", received_by=sam)
        split.settle_up(user_id)
        split.archive_person(user_id, alex.id)  # alex was the usual receiver
        split.undo_last_settlement(user_id)
        assert not [p for p in split.list_people(user_id) if p.receives_money_default]
        assert third.id

    def test_keeps_an_audit_record_of_what_was_undone(self, user_id, product, alex, sam):
        worked_example(user_id, product, alex, sam)
        settlement = split.settle_up(user_id, actor_id=user_id)
        snapshot = settlement.snapshot
        split.update_person(user_id, alex.id, name="Alex", share_pct="60")
        split.update_person(user_id, sam.id, name="Sam", share_pct="40")
        split.undo_last_settlement(user_id, actor_id=user_id)
        entry = db.session.scalar(
            select(AuditLog).where(AuditLog.action == "split.settlement_undone")
        )
        assert entry.details["snapshot"] == snapshot
        assert entry.details["shares_before_undo"] == {str(alex.id): "60.00", str(sam.id): "40.00"}

    def test_cannot_undo_another_accounts_settlement(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)
        other = UserFactory()
        db.session.commit()
        with pytest.raises(NotFoundError):
            split.undo_last_settlement(other.id)
        assert db.session.scalar(select(func.count()).select_from(Settlement)) == 1


class TestPeople:
    def test_first_person_becomes_the_usual_receiver(self, user_id, alex, sam):
        assert alex.receives_money_default is True
        assert sam.receives_money_default is False

    def test_only_one_usual_receiver(self, user_id, alex, sam):
        split.set_usual_receiver(user_id, sam.id)
        db.session.refresh(alex)
        assert alex.receives_money_default is False
        assert sam.receives_money_default is True

    def test_adding_someone_as_the_usual_receiver_replaces_the_old_one(self, user_id, alex):
        sam = split.add_person(user_id, name="Sam", share_pct="0", usually_receives_money=True)
        db.session.refresh(alex)
        assert sam.receives_money_default is True
        assert alex.receives_money_default is False

    @pytest.mark.parametrize(
        ("field", "values"),
        [
            ("name", {"name": "", "share_pct": "50"}),
            ("name", {"name": "x" * 51, "share_pct": "50"}),
            ("share_pct", {"name": "A", "share_pct": "-1"}),
            ("share_pct", {"name": "A", "share_pct": "100.01"}),
            ("share_pct", {"name": "A", "share_pct": "NaN"}),
            ("share_pct", {"name": "A", "share_pct": "Infinity"}),
            ("share_pct", {"name": "A", "share_pct": "33.333"}),
            ("share_pct", {"name": "A", "share_pct": ""}),
            ("share_pct", {"name": "A", "share_pct": 50.5}),
        ],
    )
    def test_rejects_bad_input(self, user_id, field, values):
        with pytest.raises(ValidationFailed) as error:
            split.add_person(user_id, **values)
        assert field in error.value.errors

    def test_names_are_unique_among_active_people(self, user_id, alex):
        with pytest.raises(ConflictError):
            split.add_person(user_id, name="ALEX", share_pct="1")

    def test_rename_clash(self, user_id, alex, sam):
        with pytest.raises(ConflictError):
            split.update_person(user_id, sam.id, name="alex", share_pct="50")
        assert split.get_person(user_id, sam.id).name == "Sam"

    def test_even_split_totals_exactly_100(self, user_id):
        """Audit: the original gave three people 33.33 each, leaving 0.01% unallocated."""
        for name in ("A", "B", "C"):
            split.add_person(user_id, name=name, share_pct="1")
        people = split.set_even_shares(user_id)
        assert [p.share_pct for p in people] == [
            Decimal("33.34"),
            Decimal("33.33"),
            Decimal("33.33"),
        ]
        assert sum(p.share_pct for p in people) == Decimal("100.00")

    def test_even_split_with_nobody_does_nothing(self, user_id):
        assert split.set_even_shares(user_id) == []

    def test_even_split_leaves_archived_people_out(self, user_id, alex, sam):
        split.archive_person(user_id, sam.id)
        people = split.set_even_shares(user_id)
        assert [(p.name, p.share_pct) for p in people] == [("Alex", Decimal("100.00"))]

    def test_warns_once_when_shares_are_off_then_clears_it(self, user_id):
        split.add_person(user_id, name="A", share_pct="10")
        split.add_person(user_id, name="B", share_pct="20")
        unread = notifications.list_for(user_id, unread_only=True)
        assert [n.kind for n in unread] == [Kind.SHARES_NOT_100.value]

        split.add_person(user_id, name="C", share_pct="70")
        assert notifications.unread_count(user_id) == 0

    def test_even_split_clears_the_warning_too(self, user_id):
        split.add_person(user_id, name="A", share_pct="10")
        assert notifications.unread_count(user_id) == 1
        split.set_even_shares(user_id)
        assert notifications.unread_count(user_id) == 0


class TestChangingTheUsualReceiverNeverMovesOldSales:
    """Whoever received a sale stays that way, however it came to be recorded."""

    def _holding(self, user_id, person):
        return split.current_period(user_id).result.person(person.id).holding

    def test_a_sale_with_a_receiver(self, user_id, product, alex, sam):
        sell(user_id, product, "100")
        split.set_usual_receiver(user_id, sam.id)
        assert self._holding(user_id, alex) == Decimal("100.00")
        assert self._holding(user_id, sam) == Decimal("0.00")

    def test_a_sale_saved_before_anyone_was_added(self, user_id, product):
        sale = sell(user_id, product, "100")
        assert sale.received_by_partner_id is None
        alex = split.add_person(user_id, name="Alex", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        db.session.refresh(sale)
        assert sale.received_by_partner_id == alex.id

        split.set_usual_receiver(user_id, sam.id)

        assert self._holding(user_id, alex) == Decimal("100.00")
        assert self._holding(user_id, sam) == Decimal("0.00")

    def test_a_sale_saved_while_nobody_was_the_usual_receiver(self, user_id, product, alex, sam):
        third = split.add_person(user_id, name="Third", share_pct="0")
        split.archive_person(user_id, alex.id)  # several left, so nobody is chosen for them
        sale = sell(user_id, product, "100")
        assert sale.received_by_partner_id is None
        assert Issue.UNASSIGNED_SALES in split.current_period(user_id).result.issues

        split.set_usual_receiver(user_id, sam.id)
        assert self._holding(user_id, sam) == Decimal("100.00")
        assert Issue.UNASSIGNED_SALES not in split.current_period(user_id).result.issues

        split.set_usual_receiver(user_id, third.id)
        assert self._holding(user_id, sam) == Decimal("100.00")
        assert self._holding(user_id, third) == Decimal("0.00")

    def test_a_deleted_sale_that_is_restored_later(self, user_id, product):
        sale = sell(user_id, product, "100")
        sales.delete_sale(user_id, sale.id)
        alex = split.add_person(user_id, name="Alex", share_pct="50")
        sam = split.add_person(user_id, name="Sam", share_pct="50")
        split.set_usual_receiver(user_id, sam.id)

        sales.restore_sales(user_id, [sale.id])

        assert self._holding(user_id, alex) == Decimal("100.00")
        assert self._holding(user_id, sam) == Decimal("0.00")

    def test_a_new_person_chosen_as_usual_receiver(self, user_id, product, alex):
        sell(user_id, product, "100")
        sam = split.add_person(user_id, name="Sam", share_pct="50", usually_receives_money=True)
        assert self._holding(user_id, alex) == Decimal("100.00")
        assert self._holding(user_id, sam) == Decimal("0.00")

    def test_only_this_accounts_sales_are_touched(self, user_id, product):
        other = UserFactory()
        db.session.commit()
        theirs = SaleFactory(user_id=other.id, received_by_partner_id=None)
        db.session.commit()
        split.add_person(user_id, name="Alex", share_pct="100")
        db.session.refresh(theirs)
        assert theirs.received_by_partner_id is None

    def test_a_sale_in_a_settled_period_is_left_alone(self, user_id, product, alex, sam):
        """A $0 sale can settle with no receiver. Its row is locked, so it isn't rewritten."""
        split._clear_usual_receiver(user_id)
        db.session.commit()
        sale = sell(user_id, product, "0.00")
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)
        split.set_usual_receiver(user_id, alex.id)
        db.session.refresh(sale)
        assert sale.received_by_partner_id is None


class TestArchive:
    def test_blocked_while_they_have_activity(self, user_id, product, alex, sam):
        spend(user_id, sam, "10")
        with pytest.raises(ConflictError, match="Settle up first"):
            split.archive_person(user_id, sam.id)
        assert split.get_person(user_id, sam.id).archived_at is None

    @pytest.mark.parametrize("kind", ["sale", "payment_from", "payment_to"])
    def test_every_kind_of_activity_blocks_it(self, user_id, product, alex, sam, kind):
        if kind == "sale":
            sell(user_id, product, "10", received_by=sam)
        elif kind == "payment_from":
            pay(user_id, sam, alex, "1")
        else:
            pay(user_id, alex, sam, "1")
        with pytest.raises(ConflictError):
            split.archive_person(user_id, sam.id)

    def test_usual_receiver_is_blocked_by_sales_with_no_receiver(self, user_id, alex, sam):
        SaleFactory(user_id=user_id, received_by_partner_id=None)
        db.session.commit()
        with pytest.raises(ConflictError):
            split.archive_person(user_id, alex.id)

    def test_allowed_after_settling(self, user_id, product, alex, sam):
        sell(user_id, product, "10", received_by=sam)
        split.settle_up(user_id)
        archived = split.archive_person(user_id, sam.id)
        assert archived.archived_at is not None
        assert archived.share_pct == Decimal("0.00")
        assert [p.name for p in split.list_people(user_id)] == ["Alex"]
        names = [p.name for p in split.list_people(user_id, include_archived=True)]
        assert names == ["Alex", "Sam"]

    def test_their_history_is_kept(self, user_id, product, alex, sam):
        sale = sell(user_id, product, "10", received_by=sam)
        settlement = split.settle_up(user_id)
        split.archive_person(user_id, sam.id)
        db.session.refresh(sale)
        assert sale.received_by_partner_id == sam.id
        assert "Sam" in {p["name"] for p in settlement.snapshot["people"]}

    def test_only_successor_becomes_the_usual_receiver(self, user_id, alex, sam):
        split.archive_person(user_id, alex.id)
        db.session.refresh(sam)
        assert sam.receives_money_default is True

    def test_no_guessing_between_several_successors(self, user_id, alex, sam):
        third = split.add_person(user_id, name="Third", share_pct="0")
        split.archive_person(user_id, alex.id)
        db.session.refresh(sam)
        db.session.refresh(third)
        assert not sam.receives_money_default
        assert not third.receives_money_default

    def test_archived_people_are_left_out_of_a_new_period(self, user_id, product, alex, sam):
        split.archive_person(user_id, sam.id)
        split.update_person(user_id, alex.id, name="Alex", share_pct="100")
        sell(user_id, product, "10", received_by=alex)
        result = split.current_period(user_id).result
        assert [p.name for p in result.people] == ["Alex"]
        assert result.can_settle

    def test_cannot_be_used_for_new_rows(self, user_id, alex, sam):
        split.archive_person(user_id, sam.id)
        with pytest.raises(ValidationFailed):
            spend(user_id, sam, "1")
        with pytest.raises(ValidationFailed):
            pay(user_id, sam, alex, "1")
        with pytest.raises(ValidationFailed):
            pay(user_id, alex, sam, "1")

    def test_cannot_be_edited_until_restored(self, user_id, alex, sam):
        split.archive_person(user_id, sam.id)
        with pytest.raises(ValidationFailed):
            split.update_person(user_id, sam.id, name="Sam", share_pct="50")
        with pytest.raises(ValidationFailed):
            split.set_usual_receiver(user_id, sam.id)

    def test_archiving_twice_is_harmless(self, user_id, alex, sam):
        first = split.archive_person(user_id, sam.id).archived_at
        assert split.archive_person(user_id, sam.id).archived_at == first

    def test_name_can_be_reused_and_restore_is_blocked_while_taken(self, user_id, alex, sam):
        split.archive_person(user_id, sam.id)
        new_sam = split.add_person(user_id, name="sam", share_pct="0")
        with pytest.raises(ConflictError):
            split.restore_person(user_id, sam.id)
        split.delete_person(user_id, new_sam.id)
        restored = split.restore_person(user_id, sam.id)
        assert restored.archived_at is None
        assert restored.share_pct == Decimal("0.00")

    def test_restoring_someone_active_changes_nothing(self, user_id, alex, sam):
        assert split.restore_person(user_id, sam.id).receives_money_default is False

    def test_restore_fills_a_missing_usual_receiver(self, user_id, alex, sam):
        split.archive_person(user_id, alex.id)
        split.archive_person(user_id, sam.id)
        assert split.restore_person(user_id, alex.id).receives_money_default is True


class TestDeletePerson:
    def test_someone_added_by_mistake_can_be_removed(self, user_id, alex, sam):
        split.delete_person(user_id, sam.id)
        assert [p.name for p in split.list_people(user_id)] == ["Alex"]

    @pytest.mark.parametrize("kind", ["sale", "cost", "payment", "settled", "deleted_sale"])
    def test_anyone_with_history_can_only_be_archived(self, user_id, product, alex, sam, kind):
        if kind == "sale":
            sell(user_id, product, "10", received_by=sam)
        elif kind == "cost":
            spend(user_id, sam, "10")
        elif kind == "payment":
            pay(user_id, alex, sam, "10")
        elif kind == "settled":
            sell(user_id, product, "10", received_by=alex)
            split.settle_up(user_id)  # Sam has no rows, but appears in the snapshot
        else:
            sales.delete_sale(user_id, sell(user_id, product, "10", received_by=sam).id)
        with pytest.raises(ConflictError, match="only be archived"):
            split.delete_person(user_id, sam.id)
        assert split.get_person(user_id, sam.id).id == sam.id

    def test_removing_the_usual_receiver_promotes_the_only_person_left(self, user_id, alex, sam):
        split.delete_person(user_id, alex.id)
        db.session.refresh(sam)
        assert sam.receives_money_default is True


class TestCosts:
    def test_add_with_every_field(self, user_id, alex, product):
        cost = split.add_cost(
            user_id,
            description=" Boxes ",
            category=CostCategory.PACKAGING,
            amount="$1,000.50",
            cost_date="2026-11-01",
            paid_by_partner_id=alex.id,
            product_id=product.id,
            notes=" From the market ",
        )
        assert cost.description == "Boxes"
        assert cost.category is CostCategory.PACKAGING
        assert cost.amount == Decimal("1000.50")
        assert cost.cost_date == date(2026, 11, 1)
        assert cost.product_id == product.id
        assert cost.notes == "From the market"

    @pytest.mark.parametrize(
        ("field", "changes"),
        [
            ("description", {"description": ""}),
            ("description", {"description": "x" * 201}),
            ("category", {"category": "yachts"}),
            ("amount", {"amount": "0"}),
            ("amount", {"amount": "-5"}),
            ("amount", {"amount": "NaN"}),
            ("cost_date", {"cost_date": "soon"}),
            ("notes", {"notes": "x" * 1001}),
            ("paid_by_partner_id", {"paid_by_partner_id": 999999}),
            ("product_id", {"product_id": 999999}),
        ],
    )
    def test_rejects_bad_input(self, user_id, alex, field, changes):
        values = {
            "description": "Stock",
            "category": "stock",
            "amount": "1",
            "cost_date": DAY,
            "paid_by_partner_id": alex.id,
            **changes,
        }
        with pytest.raises(ValidationFailed) as error:
            split.add_cost(user_id, **values)
        assert field in error.value.errors
        assert db.session.scalar(select(func.count()).select_from(UpfrontCost)) == 0

    def test_edit_and_delete_while_open(self, user_id, alex, sam):
        cost = spend(user_id, alex, "10")
        split.update_cost(
            user_id,
            cost.id,
            description="Stock",
            category="stock",
            amount="20",
            cost_date=DAY,
            paid_by_partner_id=sam.id,
        )
        result = split.current_period(user_id).result
        assert result.person(sam.id).gets_back == Decimal("20.00")
        split.delete_cost(user_id, cost.id)
        assert split.current_period(user_id).result.upfront_total == Decimal("0.00")

    def test_list(self, user_id, product, alex, sam):
        first = spend(user_id, alex, "1", day=date(2026, 1, 1))
        split.settle_up(user_id)
        second = spend(user_id, alex, "2", day=date(2026, 2, 1))
        assert [c.id for c in split.list_costs(user_id)] == [second.id, first.id]
        assert [c.id for c in split.list_costs(user_id, open_only=True)] == [second.id]

    def test_a_removed_product_just_unlinks(self, user_id, alex, product):
        cost = spend(user_id, alex, "10", product_id=product.id)
        products.delete_product(user_id, product.id)
        db.session.refresh(cost)
        assert cost.product_id is None


class TestPayments:
    @pytest.mark.parametrize(
        ("field", "changes"),
        [
            ("amount", {"amount": "0"}),
            ("amount", {"amount": "-1"}),
            ("amount", {"amount": "NaN"}),
            ("paid_on", {"paid_on": ""}),
            ("note", {"note": "x" * 201}),
            ("from_partner_id", {"from_partner_id": 999999}),
            ("to_partner_id", {"to_partner_id": 999999}),
        ],
    )
    def test_rejects_bad_input(self, user_id, alex, sam, field, changes):
        values = {
            "from_partner_id": alex.id,
            "to_partner_id": sam.id,
            "amount": "1",
            "paid_on": DAY,
            **changes,
        }
        with pytest.raises(ValidationFailed) as error:
            split.record_payment(user_id, **values)
        assert field in error.value.errors
        assert db.session.scalar(select(func.count()).select_from(PartnerPayment)) == 0

    def test_cannot_pay_yourself(self, user_id, alex):
        with pytest.raises(ValidationFailed) as error:
            pay(user_id, alex, alex, "1")
        assert "different people" in error.value.errors["to_partner_id"]

    def test_edit_and_delete_while_open(self, user_id, alex, sam):
        payment = pay(user_id, alex, sam, "10")
        updated = split.update_payment(
            user_id,
            payment.id,
            from_partner_id=sam.id,
            to_partner_id=alex.id,
            amount="4",
            paid_on=DAY,
            note="Reversed",
        )
        assert updated.amount == Decimal("4.00")
        assert split.current_period(user_id).result.person(alex.id).holding == Decimal("4.00")
        split.delete_payment(user_id, payment.id)
        assert split.current_period(user_id).row_count == 0

    def test_editing_cannot_make_it_to_yourself(self, user_id, alex, sam):
        payment = pay(user_id, alex, sam, "10")
        with pytest.raises(ValidationFailed):
            split.update_payment(
                user_id,
                payment.id,
                from_partner_id=alex.id,
                to_partner_id=alex.id,
                amount="10",
                paid_on=DAY,
            )

    def test_list(self, user_id, alex, sam):
        first = pay(user_id, alex, sam, "1", day=date(2026, 1, 1))
        split.settle_up(user_id)
        second = pay(user_id, alex, sam, "2", day=date(2026, 2, 1))
        assert [p.id for p in split.list_payments(user_id)] == [second.id, first.id]
        assert [p.id for p in split.list_payments(user_id, open_only=True)] == [second.id]


class TestOwnership:
    """Nobody can reach or reference another account's split records."""

    @pytest.fixture
    def theirs(self, ctx):
        owner = UserFactory()
        db.session.commit()
        pat = split.add_person(owner.id, name="Pat", share_pct="50")
        kim = split.add_person(owner.id, name="Kim", share_pct="50")
        cost = spend(owner.id, pat, "5")
        payment = pay(owner.id, pat, kim, "5")
        settlement_owner = owner.id
        return {
            "pat": pat,
            "kim": kim,
            "cost": cost,
            "payment": payment,
            "owner": settlement_owner,
        }

    def test_cannot_reach_their_records(self, user_id, theirs):
        pat, kim, cost, payment = theirs["pat"], theirs["kim"], theirs["cost"], theirs["payment"]
        settlement = split.settle_up(theirs["owner"])
        calls = [
            lambda: split.get_person(user_id, pat.id),
            lambda: split.update_person(user_id, pat.id, name="Hacked", share_pct="1"),
            lambda: split.set_usual_receiver(user_id, pat.id),
            lambda: split.archive_person(user_id, pat.id),
            lambda: split.restore_person(user_id, pat.id),
            lambda: split.delete_person(user_id, kim.id),
            lambda: split.get_cost(user_id, cost.id),
            lambda: split.delete_cost(user_id, cost.id),
            lambda: split.get_payment(user_id, payment.id),
            lambda: split.delete_payment(user_id, payment.id),
            lambda: split.get_settlement(user_id, settlement.id),
        ]
        for call in calls:
            with pytest.raises(NotFoundError):
                call()
        assert db.session.get(Partner, pat.id).name == "Pat"

    def test_cannot_reference_their_people(self, user_id, alex, sam, theirs):
        with pytest.raises(ValidationFailed):
            split.add_cost(
                user_id,
                description="x",
                category="stock",
                amount="1",
                cost_date=DAY,
                paid_by_partner_id=theirs["pat"].id,
            )
        with pytest.raises(ValidationFailed):
            split.record_payment(
                user_id,
                from_partner_id=alex.id,
                to_partner_id=theirs["pat"].id,
                amount="1",
                paid_on=DAY,
            )

    def test_cannot_link_a_cost_to_their_product(self, user_id, alex, theirs):
        their_product = products.create_product(theirs["owner"], name="Theirs", cost="1")
        with pytest.raises(ValidationFailed):
            spend(user_id, alex, "1", product_id=their_product.id)

    def test_lists_and_sums_are_separate(self, user_id, alex, theirs):
        assert split.list_costs(user_id) == []
        assert split.list_payments(user_id) == []
        assert [p.name for p in split.list_people(user_id)] == ["Alex"]
        assert split.current_period(user_id).result.upfront_total == Decimal("0.00")


def test_sales_and_costs_and_payments_models_are_stamped_together(user_id, product, alex, sam):
    spend(user_id, alex, "1")
    pay(user_id, alex, sam, "1")
    sell(user_id, product, "1", received_by=alex)
    settlement = split.settle_up(user_id)
    for model in (Sale, UpfrontCost, PartnerPayment):
        assert db.session.scalar(select(model.settlement_id)) == settlement.id
