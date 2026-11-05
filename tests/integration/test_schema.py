"""The database itself refuses bad data, even if a bug gets past the services."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import downgrade, upgrade
from sqlalchemy.exc import DataError, IntegrityError

from app.extensions import db
from app.models import (
    AuditLog,
    FeatureFlag,
    Notification,
    Partner,
    PartnerPayment,
    Product,
    ProductCostChange,
    RecoveryCode,
    Sale,
    Settlement,
    SignIn,
    SupportMessage,
    UpfrontCost,
)
from app.services import products, sales, split
from tests.factories import (
    PartnerFactory,
    PaymentFactory,
    ProductFactory,
    SaleFactory,
    SettlementFactory,
    UpfrontCostFactory,
    UserFactory,
)

pytestmark = pytest.mark.integration

NAN = Decimal("NaN")


@pytest.fixture
def user(ctx):
    user = UserFactory()
    db.session.commit()
    return user


def flush_fails(error=IntegrityError):
    """The next flush or commit must fail. The transaction is rolled back by the fixture."""
    return pytest.raises(error)


class TestMoneyColumns:
    """Audit: one crafted POST with NaN made every total NaN. Postgres treats NaN >= 0 as true."""

    @pytest.mark.parametrize("value", [NAN, Decimal("-0.01")])
    @pytest.mark.parametrize("column", ["price_paid", "cost_each"])
    def test_sales(self, user, column, value):
        with flush_fails():
            SaleFactory(user_id=user.id, **{column: value})

    @pytest.mark.parametrize("value", [NAN, Decimal("-1")])
    @pytest.mark.parametrize("column", ["cost", "sell_price"])
    def test_products(self, user, column, value):
        with flush_fails():
            ProductFactory(user_id=user.id, **{column: value})

    @pytest.mark.parametrize("value", [NAN, Decimal("0"), Decimal("-1")])
    def test_upfront_costs(self, user, value):
        person = PartnerFactory(user_id=user.id)
        with flush_fails():
            UpfrontCostFactory(user_id=user.id, paid_by_partner_id=person.id, amount=value)

    @pytest.mark.parametrize("value", [NAN, Decimal("0"), Decimal("-1")])
    def test_payments(self, user, value):
        a = PartnerFactory(user_id=user.id)
        b = PartnerFactory(user_id=user.id)
        with flush_fails():
            PaymentFactory(user_id=user.id, from_partner_id=a.id, to_partner_id=b.id, amount=value)

    @pytest.mark.parametrize("value", [NAN, Decimal("-1"), Decimal("100.01")])
    def test_shares(self, user, value):
        with flush_fails():
            PartnerFactory(user_id=user.id, share_pct=value)

    def test_more_than_12_digits_does_not_fit(self, user):
        with flush_fails(DataError):
            SaleFactory(user_id=user.id, price_paid=Decimal("100000000000.00"))

    def test_infinity_does_not_fit(self, user):
        with flush_fails(DataError):
            SaleFactory(user_id=user.id, price_paid=Decimal("Infinity"))


class TestOtherChecks:
    @pytest.mark.parametrize("quantity", [0, -1])
    def test_quantity_must_be_positive(self, user, quantity):
        with flush_fails():
            SaleFactory(user_id=user.id, quantity=quantity)

    def test_cannot_pay_yourself(self, user):
        person = PartnerFactory(user_id=user.id)
        with flush_fails():
            PaymentFactory(user_id=user.id, from_partner_id=person.id, to_partner_id=person.id)

    @pytest.mark.parametrize("username", ["ab", "a" * 31, "has space", "a@b", "<x>"])
    def test_username_format(self, ctx, username):
        with flush_fails():
            UserFactory(username=username)

    def test_usernames_are_unique_ignoring_case(self, ctx):
        UserFactory(username="Alex")
        with flush_fails():
            UserFactory(username="aLEX")

    def test_rollout_percentage_range(self, ctx):
        db.session.add(FeatureFlag(key="x", rollout_pct=101))
        with flush_fails():
            db.session.flush()

    def test_calculated_columns_cannot_be_written(self, user):
        sale = SaleFactory(user_id=user.id, price_paid=Decimal("10.00"), cost_each=Decimal("1.00"))
        db.session.commit()
        with pytest.raises(sa.exc.ProgrammingError):
            db.session.execute(
                sa.text("UPDATE sales SET profit = 1000 WHERE id = :id"), {"id": sale.id}
            )

    def test_calculated_columns_use_exact_arithmetic(self, user):
        sale = SaleFactory(
            user_id=user.id,
            quantity=3,
            cost_each=Decimal("0.70"),
            price_paid=Decimal("2.10"),
        )
        db.session.commit()
        assert sale.total_cost == Decimal("2.10")
        assert sale.profit == Decimal("0.00")

    def test_two_people_cannot_both_be_the_usual_receiver(self, user):
        PartnerFactory(user_id=user.id, receives_money_default=True)
        with flush_fails():
            PartnerFactory(user_id=user.id, receives_money_default=True)

    def test_different_accounts_each_have_their_own_usual_receiver(self, user):
        other = UserFactory()
        PartnerFactory(user_id=user.id, receives_money_default=True)
        PartnerFactory(user_id=other.id, receives_money_default=True)
        db.session.commit()


class TestRowsStayInTheirOwnAccount:
    """A last line of defence: even a buggy query can't link rows across accounts."""

    @pytest.fixture
    def other(self, ctx):
        other = UserFactory()
        partner = PartnerFactory(user_id=other.id)
        settlement = SettlementFactory(user_id=other.id)
        db.session.commit()
        return partner, settlement

    def test_sale_received_by_someone_elses_person(self, user, other):
        SaleFactory(user_id=user.id, received_by_partner_id=other[0].id)
        with flush_fails():
            db.session.commit()

    def test_sale_in_someone_elses_settlement(self, user, other):
        SaleFactory(user_id=user.id, settlement_id=other[1].id)
        with flush_fails():
            db.session.commit()

    def test_cost_paid_by_someone_elses_person(self, user, other):
        UpfrontCostFactory(user_id=user.id, paid_by_partner_id=other[0].id)
        with flush_fails():
            db.session.commit()

    def test_cost_in_someone_elses_settlement(self, user, other):
        mine = PartnerFactory(user_id=user.id)
        UpfrontCostFactory(user_id=user.id, paid_by_partner_id=mine.id, settlement_id=other[1].id)
        with flush_fails():
            db.session.commit()

    def test_payment_with_someone_elses_person(self, user, other):
        mine = PartnerFactory(user_id=user.id)
        PaymentFactory(user_id=user.id, from_partner_id=mine.id, to_partner_id=other[0].id)
        with flush_fails():
            db.session.commit()

    def test_payment_in_someone_elses_settlement(self, user, other):
        a = PartnerFactory(user_id=user.id)
        b = PartnerFactory(user_id=user.id)
        PaymentFactory(
            user_id=user.id, from_partner_id=a.id, to_partner_id=b.id, settlement_id=other[1].id
        )
        with flush_fails():
            db.session.commit()

    def test_person_with_history_cannot_be_deleted_directly(self, user):
        person = PartnerFactory(user_id=user.id)
        UpfrontCostFactory(user_id=user.id, paid_by_partner_id=person.id)
        db.session.commit()
        db.session.delete(person)
        with flush_fails():
            db.session.commit()


class TestDeletingAnAccount:
    def test_removes_everything_it_owns_and_nothing_else(self, ctx):
        """A diamond of foreign keys: people and settlements are referenced by many tables."""
        doomed, kept = UserFactory(), UserFactory()
        db.session.commit()

        for owner in (doomed, kept):
            alex = split.add_person(owner.id, name="Alex", share_pct="50")
            sam = split.add_person(owner.id, name="Sam", share_pct="50")
            product = products.create_product(owner.id, name="Stamp", cost="1")
            day = date(2026, 11, 5)
            for _ in range(2):  # one settled period and one open one
                sales.create_sale(
                    owner.id,
                    product_id=product.id,
                    sale_date=day,
                    quantity=1,
                    price_paid="10",
                    received_by_partner_id=sam.id,
                )
                split.add_cost(
                    owner.id,
                    description="Stock",
                    category="stock",
                    amount="1",
                    cost_date=day,
                    paid_by_partner_id=alex.id,
                    product_id=product.id,
                )
                split.record_payment(
                    owner.id,
                    from_partner_id=sam.id,
                    to_partner_id=alex.id,
                    amount="1",
                    paid_on=day,
                )
                split.settle_up(owner.id)
            db.session.add(SupportMessage(user_id=owner.id, subject="Hi", body="Hello"))
            db.session.add(SignIn(user_id=owner.id))
            db.session.add(RecoveryCode(user_id=owner.id, code_hash=f"hash-{owner.id}"))
            db.session.add(AuditLog(action="something", actor_id=owner.id))
            db.session.commit()

        doomed_id, kept_id = doomed.id, kept.id
        db.session.delete(doomed)
        db.session.commit()

        owned = (
            Partner,
            Product,
            Sale,
            Settlement,
            UpfrontCost,
            PartnerPayment,
            Notification,
            SupportMessage,
            SignIn,
            RecoveryCode,
        )
        for model in owned:
            mine = db.session.scalar(
                sa.select(sa.func.count()).select_from(model).where(model.user_id == doomed_id)
            )
            theirs = db.session.scalar(
                sa.select(sa.func.count()).select_from(model).where(model.user_id == kept_id)
            )
            assert mine == 0, f"{model.__name__} rows left behind"
            assert theirs > 0, f"{model.__name__} rows of another account were removed"

        histories = db.session.scalar(sa.select(sa.func.count()).select_from(ProductCostChange))
        assert histories == 1, "only the other account's cost history remains"

        trail = db.session.scalars(sa.select(AuditLog).where(AuditLog.action == "something")).all()
        assert sorted(entry.actor_id or 0 for entry in trail) == [0, kept_id]


class TestMigrations:
    def test_the_migrations_build_exactly_what_the_models_describe(self, app):
        with app.app_context(), db.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, db.metadata)
        assert differences == []

    def test_every_table_is_covered(self, app):
        with app.app_context():
            tables = set(sa.inspect(db.engine).get_table_names())
        expected = {t.name for t in db.metadata.sorted_tables} | {"alembic_version"}
        assert tables == expected

    def test_can_go_down_and_back_up(self, app):
        with app.app_context():
            downgrade(revision="base")
            inspector = sa.inspect(db.engine)
            assert set(inspector.get_table_names()) == {"alembic_version"}
            with db.engine.connect() as connection:
                types = connection.execute(
                    sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")
                ).all()
            assert types == [], "enum types must not be left behind"
            upgrade()
            assert "sales" in sa.inspect(db.engine).get_table_names()

    def test_upgrade_twice_is_harmless(self, app):
        with app.app_context():
            upgrade()

    def test_search_indexes_exist(self, app):
        with app.app_context(), db.engine.connect() as connection:
            extensions = connection.execute(sa.text("SELECT extname FROM pg_extension")).scalars()
            names = set(extensions)
        assert {"citext", "pg_trgm"} <= names
