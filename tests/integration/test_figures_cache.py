"""Dashboard figures in Redis. The thing that must never happen is showing a wrong figure."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import redis
from sqlalchemy import update

from app import create_app, figures_cache
from app.extensions import JsonSerializer, cache, db
from app.models import Sale
from app.services import auth, products, reporting, sales, split
from app.services.reporting import Period
from tests.conftest import login, make_settings, redis_url
from tests.factories import PASSWORD, UserFactory
from tests.helpers import count_queries

pytestmark = pytest.mark.integration

DAY = "2026-11-05"
ALL_TIME = Period("all_time", None, None)
NOVEMBER = Period("this_month", date(2026, 11, 1), date(2026, 11, 30))
TODAY = date(2026, 11, 5)


@pytest.fixture
def account(ctx):
    user = UserFactory()
    db.session.commit()
    product = products.create_product(user.id, name="Stamp", cost="2.00", sell_price="5.00")
    alex = split.add_person(user.id, name="Alex", share_pct="50")
    sam = split.add_person(user.id, name="Sam", share_pct="50")
    return {"id": user.id, "product": product.id, "alex": alex.id, "sam": sam.id}


def sell(account, price="10.00", day=DAY):
    return sales.create_sale(
        account["id"],
        product_id=account["product"],
        sale_date=day,
        quantity=1,
        price_paid=price,
        received_by_partner_id=account["sam"],
    )


def fresh(user_id):
    """Everything worked out from the database, ignoring the cache."""
    return (
        reporting._work_out_totals(user_id, ALL_TIME),
        reporting._work_out_totals(user_id, NOVEMBER),
        reporting._work_out_by_product(user_id, ALL_TIME, None),
        reporting._work_out_series(user_id, NOVEMBER, TODAY),
    )


def through_cache(user_id):
    return (
        reporting.totals_for(user_id, ALL_TIME),
        reporting.totals_for(user_id, NOVEMBER),
        reporting.by_product(user_id, ALL_TIME),
        reporting.series(user_id, NOVEMBER, today=TODAY),
    )


class TestReading:
    def test_the_second_read_doesnt_touch_the_database(self, account):
        sell(account)
        first = through_cache(account["id"])
        with count_queries() as queries:
            second = through_cache(account["id"])
        assert not queries
        assert second == first

    def test_comes_back_exactly_the_same(self, account):
        sell(account, "10.10")
        sell(account, "0.05", day="2026-11-01")
        expected = fresh(account["id"])
        through_cache(account["id"])
        again = through_cache(account["id"])
        assert again == expected
        assert isinstance(again[0].money_in, Decimal)
        assert str(again[0].money_in) == "10.15"
        assert isinstance(again[3][1][0].day, date)

    def test_an_empty_account(self, account):
        through_cache(account["id"])
        assert through_cache(account["id"]) == fresh(account["id"])

    def test_periods_dont_mix(self, account):
        sell(account, day="2026-10-05")
        sell(account, day=DAY)
        assert reporting.totals_for(account["id"], ALL_TIME).sales_count == 2
        assert reporting.totals_for(account["id"], NOVEMBER).sales_count == 1

    def test_a_chart_up_to_a_different_today_doesnt_mix(self, account):
        sell(account, day="2026-11-02")
        short = reporting.series(account["id"], NOVEMBER, today=date(2026, 11, 3))
        long = reporting.series(account["id"], NOVEMBER, today=date(2026, 11, 20))
        assert len(short[1]) == 3
        assert len(long[1]) == 20

    def test_limits_dont_mix(self, account):
        other = products.create_product(account["id"], name="Other", cost="1")
        sell(account)
        sales.create_sale(
            account["id"],
            product_id=other.id,
            sale_date=DAY,
            quantity=1,
            price_paid="3",
            received_by_partner_id=account["sam"],
        )
        assert len(reporting.by_product(account["id"], ALL_TIME, limit=1)) == 1
        assert len(reporting.by_product(account["id"], ALL_TIME)) == 2

    def test_accounts_dont_mix(self, account):
        sell(account)
        other = UserFactory()
        db.session.commit()
        through_cache(account["id"])
        assert reporting.totals_for(other.id, ALL_TIME).sales_count == 0
        assert reporting.by_product(other.id, ALL_TIME) == []


class TestEveryWriteShowsUpStraightAway:
    """Each step changes something, and the figures must match the database right after."""

    def check(self, account):
        assert through_cache(account["id"]) == fresh(account["id"])
        # And they really were cached, or this wouldn't be testing anything.
        with count_queries() as queries:
            through_cache(account["id"])
        assert not queries

    def test_all_the_ways_of_changing_figures(self, account):
        user_id = account["id"]
        self.check(account)

        sale = sell(account, "10.00")
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).money_in == Decimal("10.00")

        sales.update_sale(
            user_id,
            sale.id,
            sale_date=DAY,
            quantity=2,
            price_paid="25.00",
            received_by_partner_id=account["sam"],
        )
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).money_in == Decimal("25.00")

        sales.delete_sale(user_id, sale.id)
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).sales_count == 0

        sales.restore_sales(user_id, [sale.id])
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).sales_count == 1

        sales.delete_sales(user_id, [sale.id])
        self.check(account)
        sales.restore_sales(user_id, [sale.id])
        self.check(account)

        cost = split.add_cost(
            user_id,
            description="Stall",
            category="fees",
            amount="7.00",
            cost_date=DAY,
            paid_by_partner_id=account["alex"],
        )
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).upfront_costs == Decimal("7.00")

        split.update_cost(
            user_id,
            cost.id,
            description="Stall",
            category="fees",
            amount="8.00",
            cost_date=DAY,
            paid_by_partner_id=account["alex"],
        )
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).upfront_costs == Decimal("8.00")

        split.delete_cost(user_id, cost.id)
        self.check(account)
        assert reporting.totals_for(user_id, ALL_TIME).upfront_costs == Decimal("0.00")

        # Renaming a product renames its sales in one statement, past the ORM's change tracking.
        products.update_product(user_id, account["product"], name="Renamed", cost="2.00")
        self.check(account)
        assert reporting.by_product(user_id, ALL_TIME)[0].name == "Renamed"

        split.settle_up(user_id, actor_id=user_id)
        self.check(account)
        split.undo_last_settlement(user_id, actor_id=user_id)
        self.check(account)

    def test_the_page_itself(self, app, client):
        with app.app_context():
            user_id = auth.register("alex", PASSWORD).user.id
            product_id = products.create_product(user_id, name="Stamp", cost="2").id
            person_id = split.add_person(user_id, name="Alex", share_pct="100").id
        login(client, "alex", PASSWORD)
        assert "$0.00" in client.get("/app?period=all_time").text

        response = client.post(
            "/app/sales/new",
            data={
                "product_id": product_id,
                "sale_date": DAY,
                "quantity": "1",
                "price_paid": "123.45",
                "received_by_partner_id": person_id,
            },
        )
        assert response.status_code == 302
        assert "$123.45" in client.get("/app?period=all_time").text

        client.post("/app/sales/1/delete")
        assert "$123.45" not in client.get("/app?period=all_time").text

    def test_a_rollback_changes_nothing(self, account):
        sell(account)
        through_cache(account["id"])
        generation = cache.get(f"generation:{account['id']}")
        sale = db.session.query(Sale).first()
        sale.notes = "not saved"
        db.session.flush()
        db.session.rollback()
        assert cache.get(f"generation:{account['id']}") == generation

    def test_a_change_to_one_account_keeps_anothers_figures(self, account):
        other = UserFactory()
        db.session.commit()
        other_id = other.id
        reporting.totals_for(other_id, ALL_TIME)
        sell(account)
        with count_queries() as queries:
            reporting.totals_for(other_id, ALL_TIME)
        assert not queries

    def test_figures_worked_out_before_a_write_cant_be_read_after_it(self, account):
        """Two requests overlapping: the slower one saves figures that are already out of date."""
        user_id = account["id"]
        sell(account, "10.00")
        calls = []

        def compute_then_someone_writes():
            value = reporting._work_out_totals(user_id, ALL_TIME)
            sell(account, "5.00")  # commits while the other request is still working
            calls.append(True)
            return value

        stale = figures_cache.remember(
            user_id,
            "totals",
            f"{ALL_TIME.start}:{ALL_TIME.end}",
            compute_then_someone_writes,
            reporting._encode_totals,
            reporting._decode_totals,
        )
        assert calls
        assert stale.money_in == Decimal("10.00")
        assert reporting.totals_for(user_id, ALL_TIME).money_in == Decimal("15.00")

    def test_a_direct_update_needs_forgetting_by_hand(self, account):
        """Documents the one gap: a statement with no record behind it can't be noticed."""
        sell(account, "10.00")
        through_cache(account["id"])
        db.session.execute(update(Sale).values(notes="x"))
        db.session.commit()
        figures_cache.forget(account["id"])
        assert through_cache(account["id"]) == fresh(account["id"])


class TestWhatsInRedis:
    def test_its_json_and_expires(self, account):
        sell(account)
        through_cache(account["id"])
        client = redis.Redis.from_url(redis_url())
        keys = [key for key in client.keys("*") if b":cache:" in key]
        assert keys
        for key in keys:
            value = client.get(key)
            assert not value.startswith(b"!"), "pickle"
            json.loads(value)
            if b"generation" not in key:
                assert 0 < client.ttl(key) <= 300

    def test_serializer(self):
        serializer = JsonSerializer()
        assert serializer.loads(serializer.dumps({"a": [1, "2"]})) == {"a": [1, "2"]}
        assert serializer.loads(None) is None

    def test_a_pickle_in_redis_is_never_run(self, account):
        """If someone gets into Redis, the most they can do is spoil a figure for a moment."""
        sell(account)
        client = redis.Redis.from_url(redis_url())
        through_cache(account["id"])
        for key in client.keys("*totals*"):
            client.set(key, b"!" + b"cos\nsystem\n(S'touch /tmp/pwned'\ntR.")
        assert through_cache(account["id"]) == fresh(account["id"])

    @pytest.mark.parametrize("junk", [b'"text"', b"[1,2]", b'["x","y","z","q",1,2]', b"{}", b"12"])
    def test_nonsense_counts_as_nothing_saved(self, account, junk):
        sell(account)
        client = redis.Redis.from_url(redis_url())
        through_cache(account["id"])
        for key in client.keys("*totals*"):
            client.set(key, junk)
        assert reporting.totals_for(account["id"], ALL_TIME) == fresh(account["id"])[0]


class TestWithoutRedis:
    @pytest.fixture(autouse=True)
    def _put_the_rate_limiter_back(self):
        """Making an app points the shared limiter at its Redis, so it has to be pointed back."""
        yield
        create_app(make_settings())

    def test_redis_going_away_only_makes_it_slower(self):
        application = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
        with application.app_context():
            user = UserFactory()
            db.session.commit()
            product = products.create_product(user.id, name="Stamp", cost="2")
            person = split.add_person(user.id, name="Alex", share_pct="100")
            account = {"id": user.id, "product": product.id, "sam": person.id}
            sell(account)
            assert through_cache(user.id) == fresh(user.id)
            sell(account)
            assert reporting.totals_for(user.id, ALL_TIME).sales_count == 2
            db.session.remove()

    def test_no_redis_configured(self):
        application = create_app(make_settings(redis_url=None))
        with application.app_context():
            user = UserFactory()
            db.session.commit()
            assert reporting.totals_for(user.id, ALL_TIME).sales_count == 0
            figures_cache.forget(user.id)
            db.session.remove()

    def test_saving_can_fail_without_breaking_the_page(self, account, monkeypatch):
        sell(account)

        def refuse(*_args, **_kwargs):
            raise redis.ConnectionError

        monkeypatch.setattr(cache, "set", refuse)
        assert reporting.totals_for(account["id"], ALL_TIME).sales_count == 1
