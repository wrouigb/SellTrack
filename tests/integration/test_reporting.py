"""Dashboard figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import User
from app.services import products, reporting, sales
from app.services.errors import ValidationFailed
from app.services.reporting import Period, previous_period, resolve_period
from tests.factories import (
    PartnerFactory,
    SaleFactory,
    UpfrontCostFactory,
    UserFactory,
)
from tests.helpers import count_queries

pytestmark = pytest.mark.integration

ALL_TIME = Period("all_time", None, None)


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


def sale(user_id, price, *, cost="2.00", quantity=1, day=date(2026, 11, 5), name="Stamp"):
    return SaleFactory(
        user_id=user_id,
        price_paid=Decimal(price),
        cost_each=Decimal(cost),
        quantity=quantity,
        sale_date=day,
        product_name=name,
    )


class TestTotals:
    def test_adds_up_like_the_original_dashboard(self, user_id):
        sale(user_id, "10.00", quantity=2)
        sale(user_id, "5.50", cost="1.25")
        db.session.commit()
        totals = reporting.totals_for(user_id, ALL_TIME)
        assert totals.money_in == Decimal("15.50")
        assert totals.cost_of_items == Decimal("5.25")
        assert totals.profit == Decimal("10.25")
        assert totals.items_sold == 3
        assert totals.sales_count == 2
        assert totals.average_sale == Decimal("7.75")

    def test_nothing_sold_yet(self, user_id):
        totals = reporting.totals_for(user_id, ALL_TIME)
        assert totals.money_in == Decimal("0.00")
        assert totals.profit == Decimal("0.00")
        assert totals.items_sold == 0
        assert totals.average_sale == Decimal("0.00")

    def test_average_rounds_half_up_to_the_cent(self, user_id):
        for price in ("0.01", "0.01", "0.03"):
            sale(user_id, price, cost="0.00")
        db.session.commit()
        assert reporting.totals_for(user_id, ALL_TIME).average_sale == Decimal("0.02")

    def test_includes_upfront_costs_in_the_period(self, user_id):
        person = PartnerFactory(user_id=user_id, share_pct=Decimal("100"))
        UpfrontCostFactory(
            user_id=user_id,
            paid_by_partner_id=person.id,
            amount=Decimal("30.00"),
            cost_date=date(2026, 11, 2),
        )
        UpfrontCostFactory(
            user_id=user_id,
            paid_by_partner_id=person.id,
            amount=Decimal("99.00"),
            cost_date=date(2026, 10, 2),
        )
        db.session.commit()
        november = Period("custom", date(2026, 11, 1), date(2026, 11, 30))
        assert reporting.totals_for(user_id, november).upfront_costs == Decimal("30.00")

    def test_leaves_out_deleted_sales_and_other_accounts(self, user_id):
        other = UserFactory()
        sale(other.id, "500.00")
        kept = sale(user_id, "10.00")
        gone = sale(user_id, "20.00")
        db.session.commit()
        sales.delete_sale(user_id, gone.id)
        totals = reporting.totals_for(user_id, ALL_TIME)
        assert totals.money_in == kept.price_paid
        assert totals.sales_count == 1

    def test_only_counts_the_period_inclusive(self, user_id):
        sale(user_id, "1.00", day=date(2026, 10, 31))
        sale(user_id, "2.00", day=date(2026, 11, 1))
        sale(user_id, "4.00", day=date(2026, 11, 30))
        sale(user_id, "8.00", day=date(2026, 12, 1))
        db.session.commit()
        period = Period("custom", date(2026, 11, 1), date(2026, 11, 30))
        assert reporting.totals_for(user_id, period).money_in == Decimal("6.00")

    def test_query_count_does_not_grow_with_sales(self, user_id):
        """Audit: the original loaded every sale on each dashboard request."""
        for _ in range(50):
            sale(user_id, "1.00")
        db.session.commit()
        with count_queries() as queries:
            summary = reporting.summary(user_id, resolve_period("this_month", date(2026, 11, 5)))
            reporting.by_product(user_id, ALL_TIME)
            reporting.recent_sales(user_id)
        assert summary.totals.sales_count == 50
        assert len(queries) <= 6


class TestSummary:
    def test_compares_with_the_previous_period(self, user_id):
        sale(user_id, "150.00", cost="0.00", day=date(2026, 11, 5))
        sale(user_id, "100.00", cost="0.00", day=date(2026, 10, 5))
        db.session.commit()
        summary = reporting.summary(user_id, resolve_period("this_month", date(2026, 11, 5)))
        change = summary.changes["money_in"]
        assert change.current == Decimal("150.00")
        assert change.previous == Decimal("100.00")
        assert change.amount == Decimal("50.00")
        assert change.percent == Decimal("50.0")
        assert summary.previous_period.start == date(2026, 10, 1)
        assert summary.previous_period.end == date(2026, 10, 31)

    def test_no_percentage_against_zero(self, user_id):
        sale(user_id, "10.00", day=date(2026, 11, 5))
        db.session.commit()
        summary = reporting.summary(user_id, resolve_period("this_month", date(2026, 11, 5)))
        assert summary.changes["money_in"].percent is None

    def test_percentage_against_a_previous_loss_keeps_its_direction(self):
        change = reporting.change_between(Decimal("50.00"), Decimal("-100.00"))
        assert change.amount == Decimal("150.00")
        assert change.percent == Decimal("150.0"), "an improvement stays a positive change"

    def test_all_time_has_nothing_to_compare_with(self, user_id):
        summary = reporting.summary(user_id, ALL_TIME)
        assert summary.previous_period is None
        assert summary.previous_totals is None
        assert summary.changes == {}


class TestPeriods:
    today = date(2026, 3, 15)

    @pytest.mark.parametrize(
        ("key", "start", "end"),
        [
            ("this_month", date(2026, 3, 1), date(2026, 3, 31)),
            ("last_month", date(2026, 2, 1), date(2026, 2, 28)),
            ("last_30_days", date(2026, 2, 14), date(2026, 3, 15)),
            ("this_year", date(2026, 1, 1), date(2026, 12, 31)),
            ("all_time", None, None),
        ],
    )
    def test_resolving(self, key, start, end):
        period = resolve_period(key, self.today)
        assert (period.start, period.end) == (start, end)

    def test_last_month_across_new_year(self):
        period = resolve_period("last_month", date(2026, 1, 20))
        assert (period.start, period.end) == (date(2025, 12, 1), date(2025, 12, 31))

    def test_this_month_in_a_leap_february(self):
        assert resolve_period("this_month", date(2028, 2, 10)).end == date(2028, 2, 29)

    def test_custom(self):
        period = resolve_period("custom", self.today, start=date(2026, 1, 5), end=date(2026, 1, 9))
        assert (period.start, period.end) == (date(2026, 1, 5), date(2026, 1, 9))

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"start": date(2026, 1, 1)}, {"start": date(2026, 2, 1), "end": date(2026, 1, 1)}],
    )
    def test_bad_custom_ranges(self, kwargs):
        with pytest.raises(ValidationFailed):
            resolve_period("custom", self.today, **kwargs)

    def test_unknown_period(self):
        with pytest.raises(ValidationFailed):
            resolve_period("yesterdayish", self.today)

    def test_previous_of_a_custom_range_is_the_same_length_before_it(self):
        before = previous_period(Period("custom", date(2026, 1, 11), date(2026, 1, 20)))
        assert (before.start, before.end) == (date(2026, 1, 1), date(2026, 1, 10))

    def test_previous_of_a_month_is_the_whole_month_before(self):
        before = previous_period(resolve_period("this_month", date(2026, 3, 15)))
        assert (before.start, before.end) == (date(2026, 2, 1), date(2026, 2, 28))

    def test_previous_of_a_year(self):
        before = previous_period(resolve_period("this_year", date(2026, 3, 15)))
        assert (before.start, before.end) == (date(2025, 1, 1), date(2025, 12, 31))


class TestToday:
    def _user(self, timezone):
        return User(username="x", password_hash="x", timezone=timezone)

    def test_uses_the_users_time_zone(self):
        moment = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
        assert reporting.today_for(self._user("Australia/Melbourne"), now=moment) == date(
            2026, 11, 6
        )
        assert reporting.today_for(self._user("America/New_York"), now=moment) == date(2026, 11, 5)

    @pytest.mark.parametrize("timezone", ["Not/AZone", "", "../../etc/passwd"])
    def test_a_bad_time_zone_falls_back_rather_than_crashing(self, timezone):
        moment = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
        assert reporting.today_for(self._user(timezone), now=moment) == date(2026, 11, 6)


class TestByProduct:
    def test_groups_by_name_best_profit_first(self, user_id):
        sale(user_id, "10.00", cost="1.00", quantity=2, name="Good")
        sale(user_id, "10.00", cost="1.00", name="Good")
        sale(user_id, "1.00", cost="5.00", name="Loss")
        sale(user_id, "6.00", cost="1.00", name="Okay")
        db.session.commit()
        rows = reporting.by_product(user_id, ALL_TIME)
        assert [(r.name, r.quantity, r.money_in, r.profit) for r in rows] == [
            ("Good", 3, Decimal("20.00"), Decimal("17.00")),
            ("Okay", 1, Decimal("6.00"), Decimal("5.00")),
            ("Loss", 1, Decimal("1.00"), Decimal("-4.00")),
        ]

    def test_limit_gives_top_products(self, user_id):
        for name, price in (("A", "1.00"), ("B", "9.00"), ("C", "5.00")):
            sale(user_id, price, cost="0.00", name=name)
        db.session.commit()
        assert [r.name for r in reporting.by_product(user_id, ALL_TIME, limit=2)] == ["B", "C"]

    def test_a_removed_product_still_shows(self, user_id):
        product = products.create_product(user_id, name="Gone", cost="1")
        sales.create_sale(
            user_id, product_id=product.id, sale_date=date(2026, 11, 5), quantity=1, price_paid="5"
        )
        products.delete_product(user_id, product.id)
        assert [r.name for r in reporting.by_product(user_id, ALL_TIME)] == ["Gone"]


class TestSeries:
    def test_daily_with_gaps_filled(self, user_id):
        sale(user_id, "10.00", cost="1.00", day=date(2026, 11, 1))
        sale(user_id, "20.00", cost="1.00", day=date(2026, 11, 3))
        db.session.commit()
        bucket, points = reporting.series(
            user_id, Period("custom", date(2026, 11, 1), date(2026, 11, 4))
        )
        assert bucket == "day"
        assert [(p.day.day, p.money_in, p.profit) for p in points] == [
            (1, Decimal("10.00"), Decimal("9.00")),
            (2, Decimal("0.00"), Decimal("0.00")),
            (3, Decimal("20.00"), Decimal("19.00")),
            (4, Decimal("0.00"), Decimal("0.00")),
        ]

    def test_monthly_over_a_long_range(self, user_id):
        sale(user_id, "10.00", cost="0.00", day=date(2026, 1, 15))
        sale(user_id, "5.00", cost="0.00", day=date(2026, 1, 31))
        sale(user_id, "7.00", cost="0.00", day=date(2026, 3, 1))
        db.session.commit()
        bucket, points = reporting.series(user_id, resolve_period("this_year", date(2026, 6, 1)))
        assert bucket == "month"
        assert len(points) == 12
        assert points[0].day == date(2026, 1, 1)
        assert points[0].money_in == Decimal("15.00")
        assert points[1].money_in == Decimal("0.00")
        assert points[2].money_in == Decimal("7.00")

    def test_all_time_uses_the_range_of_the_data(self, user_id):
        sale(user_id, "1.00", day=date(2026, 11, 1))
        sale(user_id, "1.00", day=date(2026, 11, 2))
        db.session.commit()
        bucket, points = reporting.series(user_id, ALL_TIME)
        assert bucket == "day"
        assert len(points) == 2

    def test_no_sales(self, user_id):
        assert reporting.series(user_id, ALL_TIME) == ("day", [])


def test_recent_sales_are_the_latest_five(user_id):
    for day in range(1, 8):
        sale(user_id, "1.00", day=date(2026, 11, day))
    db.session.commit()
    assert [s.sale_date.day for s in reporting.recent_sales(user_id)] == [7, 6, 5, 4, 3]
