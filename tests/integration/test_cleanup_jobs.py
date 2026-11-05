"""The nightly cleanup, and the small process that runs it."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.extensions import db
from app.jobs import cleanup, scheduler
from app.jobs.cleanup import CleanupResult, run_cleanup
from app.models import AuditLog, Notification, Sale, Settlement, SignIn, User
from app.services import auth, system
from app.services.auth import RECENT_SIGN_INS
from tests.factories import SaleFactory, UserFactory

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


def count(model):
    return db.session.scalar(select(func.count()).select_from(model))


def days_ago(days, *, seconds=0):
    return NOW - timedelta(days=days, seconds=seconds)


class TestDeletedSales:
    def test_goes_after_30_days(self, user_id):
        SaleFactory(user_id=user_id, deleted_at=days_ago(30, seconds=1))
        keep_recent = SaleFactory(user_id=user_id, deleted_at=days_ago(29))
        keep_live = SaleFactory(user_id=user_id)
        db.session.commit()
        result = run_cleanup(now=NOW)
        assert result.sales == 1
        remaining = set(db.session.scalars(select(Sale.id)))
        assert remaining == {keep_recent.id, keep_live.id}

    def test_exactly_30_days_stays(self, user_id):
        SaleFactory(user_id=user_id, deleted_at=days_ago(30))
        db.session.commit()
        assert run_cleanup(now=NOW).sales == 0

    def test_never_touches_a_settled_sale(self, user_id):
        settlement = Settlement(
            user_id=user_id,
            period_start=NOW.date(),
            period_end=NOW.date(),
            money_in=0,
            upfront_total=0,
            profit_to_share=0,
            snapshot={},
        )
        db.session.add(settlement)
        db.session.flush()
        SaleFactory(user_id=user_id, deleted_at=days_ago(90), settlement_id=settlement.id)
        db.session.commit()
        assert run_cleanup(now=NOW).sales == 0
        assert count(Sale) == 1


class TestSignIns:
    def test_old_ones_go_but_the_latest_few_stay(self, user_id):
        for number in range(RECENT_SIGN_INS + 5):
            db.session.add(
                SignIn(user_id=user_id, created_at=days_ago(100 + number), ip="10.0.0.1")
            )
        db.session.commit()
        assert run_cleanup(now=NOW).sign_ins == 5
        assert count(SignIn) == RECENT_SIGN_INS
        oldest = db.session.scalar(select(func.min(SignIn.created_at)))
        assert oldest == days_ago(100 + RECENT_SIGN_INS - 1)

    def test_recent_ones_stay_however_many(self, user_id):
        for _ in range(RECENT_SIGN_INS + 5):
            db.session.add(SignIn(user_id=user_id, created_at=days_ago(1)))
        db.session.commit()
        assert run_cleanup(now=NOW).sign_ins == 0

    def test_counted_per_account(self, user_id):
        other = UserFactory()
        db.session.flush()
        for number in range(RECENT_SIGN_INS):
            db.session.add(SignIn(user_id=user_id, created_at=days_ago(100 + number)))
            db.session.add(SignIn(user_id=other.id, created_at=days_ago(100 + number)))
        db.session.commit()
        assert run_cleanup(now=NOW).sign_ins == 0


class TestNotifications:
    def add(self, user_id, *, created, read):
        db.session.add(
            Notification(user_id=user_id, kind="x", title="t", created_at=created, read_at=read)
        )

    def test_only_read_and_old(self, user_id):
        self.add(user_id, created=days_ago(200), read=days_ago(190))  # goes
        self.add(user_id, created=days_ago(200), read=None)  # still unread
        self.add(user_id, created=days_ago(200), read=days_ago(1))  # read just now
        self.add(user_id, created=days_ago(1), read=days_ago(1))  # new
        db.session.commit()
        assert run_cleanup(now=NOW).notifications == 1
        assert count(Notification) == 3


class TestRunning:
    def test_records_that_it_ran(self, user_id):
        result = run_cleanup()
        assert result == CleanupResult(sales=0, sign_ins=0, notifications=0)
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "system.cleanup"))
        assert entry.details == {"sales": 0, "sign_ins": 0, "notifications": 0}
        assert system.last_cleanup() == entry.created_at

    def test_only_one_can_run_at_a_time(self, app, user_id):
        with db.engine.connect() as other:
            other.execute(sa.text(f"SELECT pg_advisory_xact_lock({cleanup._ADVISORY_LOCK})"))
            assert run_cleanup() is None
            other.rollback()
        assert run_cleanup() is not None

    def test_ignores_the_audit_log_and_everything_else(self, user_id):
        auth.logout_everywhere(db.session.get(User, user_id))
        before = count(AuditLog)
        run_cleanup(now=NOW + timedelta(days=3650))
        assert count(AuditLog) == before + 1
        assert db.session.get(User, user_id) is not None


class TestDueTimes:
    HOUR = 17

    def test_never_run_means_now(self):
        assert scheduler.seconds_until_due(None, NOW, hour=self.HOUR) == 0

    def test_missed_last_nights_slot_means_now(self):
        last_run = NOW - timedelta(days=2)
        assert scheduler.seconds_until_due(last_run, NOW, hour=self.HOUR) == 0

    def test_ran_last_night_waits_for_tonight(self):
        last_run = datetime(2026, 11, 4, 17, 0, 5, tzinfo=UTC)
        wait = scheduler.seconds_until_due(last_run, NOW, hour=self.HOUR)
        assert wait == 5 * 3600

    def test_just_after_the_slot_is_due(self):
        now = datetime(2026, 11, 5, 17, 0, 1, tzinfo=UTC)
        last_run = datetime(2026, 11, 4, 17, 0, 5, tzinfo=UTC)
        assert scheduler.seconds_until_due(last_run, now, hour=self.HOUR) == 0

    def test_already_run_tonight_waits_for_tomorrow(self):
        now = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)
        last_run = datetime(2026, 11, 5, 17, 0, 5, tzinfo=UTC)
        wait = scheduler.seconds_until_due(last_run, now, hour=self.HOUR)
        assert wait == 23 * 3600


class TestSchedulerLoop:
    def run_once(self, stop, monkeypatch, job):
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            stop.set()

        monkeypatch.setattr(stop, "wait", fake_wait)
        scheduler.run_forever(stop, job=job)
        return waits

    def test_runs_when_due_then_looks_again_straight_away(self, ctx, monkeypatch):
        stop = threading.Event()
        ran = []

        def job():
            ran.append(True)
            return run_cleanup()

        assert self.run_once(stop, monkeypatch, job) == [0.0]
        assert ran == [True]

    def test_waits_when_it_isnt_due(self, ctx, monkeypatch):
        run_cleanup()
        monkeypatch.setattr(scheduler, "seconds_until_due", lambda _last, _now, *, hour: 40_000.0)
        stop = threading.Event()
        ran = []
        waits = self.run_once(stop, monkeypatch, lambda: ran.append(True))
        assert not ran
        assert waits == [900.0]  # it looks again often, in case the clock moves

    def test_short_wait_is_exact(self, ctx, monkeypatch):
        monkeypatch.setattr(scheduler, "seconds_until_due", lambda _last, _now, *, hour: 60.0)
        stop = threading.Event()
        assert self.run_once(stop, monkeypatch, lambda: None) == [60.0]

    def test_if_another_process_is_running_it_doesnt_spin(self, ctx, monkeypatch):
        stop = threading.Event()
        assert self.run_once(stop, monkeypatch, lambda: None) == [900.0]

    def test_a_database_problem_is_retried_not_fatal(self, ctx, monkeypatch):
        stop = threading.Event()

        def job():
            raise OperationalError("SELECT 1", {}, Exception("database went away"))

        assert self.run_once(stop, monkeypatch, job) == [900.0]

    def test_stops_when_told(self, ctx):
        stop = threading.Event()
        stop.set()
        ran = []
        scheduler.run_forever(stop, job=lambda: ran.append(True))
        assert not ran


class TestCommands:
    def test_cleanup_command(self, app, ctx):
        result = app.test_cli_runner().invoke(args=["jobs", "cleanup"])
        assert result.exit_code == 0
        assert "Removed 0 deleted sales" in result.output

    def test_cleanup_command_when_already_running(self, app, ctx, monkeypatch):
        monkeypatch.setattr("app.cli.run_cleanup", lambda: None)
        result = app.test_cli_runner().invoke(args=["jobs", "cleanup"])
        assert result.exit_code == 1
        assert "already running" in result.output

    def test_scheduler_command_stops_on_a_signal(self, app, ctx, monkeypatch):
        import signal

        handlers = {}
        monkeypatch.setattr(
            signal, "signal", lambda number, handler: handlers.update({number: handler})
        )

        def fake_run(stop):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert stop.is_set()

        monkeypatch.setattr("app.cli.run_forever", fake_run)
        result = app.test_cli_runner().invoke(args=["jobs", "scheduler"])
        assert result.exit_code == 0, result.output
        assert signal.SIGINT in handlers
