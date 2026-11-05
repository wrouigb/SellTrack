"""In-app notifications, feature flags and the audit log."""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy import select

from app.extensions import db
from app.models import AuditLog, FeatureFlag
from app.services import audit, flags, notifications
from app.services.errors import NotFoundError, ValidationFailed
from app.services.notifications import Kind
from tests.factories import UserFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id(ctx):
    user = UserFactory()
    db.session.commit()
    return user.id


class TestNotifications:
    def test_newest_first(self, user_id):
        first = notifications.notify(user_id, Kind.ANNOUNCEMENT, "One")
        second = notifications.notify(user_id, Kind.ANNOUNCEMENT, "Two", "Body", url="/app")
        db.session.commit()
        listed = notifications.list_for(user_id)
        assert [n.id for n in listed] == [second.id, first.id]
        assert (listed[0].title, listed[0].body, listed[0].url) == ("Two", "Body", "/app")

    def test_paging(self, user_id):
        for number in range(5):
            notifications.notify(user_id, Kind.ANNOUNCEMENT, f"N{number}")
        db.session.commit()
        assert [n.title for n in notifications.list_for(user_id, limit=2, offset=1)] == ["N3", "N2"]

    def test_notify_joins_the_callers_transaction(self, user_id):
        notifications.notify(user_id, Kind.ANNOUNCEMENT, "Rolled back")
        db.session.rollback()
        assert notifications.unread_count(user_id) == 0

    def test_unread_count_and_mark_read(self, user_id):
        note = notifications.notify(user_id, Kind.ANNOUNCEMENT, "One")
        notifications.notify(user_id, Kind.ANNOUNCEMENT, "Two")
        db.session.commit()
        assert notifications.unread_count(user_id) == 2

        notifications.mark_read(user_id, note.id)
        assert notifications.unread_count(user_id) == 1
        assert [n.title for n in notifications.list_for(user_id, unread_only=True)] == ["Two"]
        assert notifications.mark_all_read(user_id) == 1
        assert notifications.unread_count(user_id) == 0

    def test_marking_read_twice_keeps_the_first_time(self, user_id):
        note = notifications.notify(user_id, Kind.ANNOUNCEMENT, "One")
        db.session.commit()
        notifications.mark_read(user_id, note.id)
        when = note.read_at
        notifications.mark_read(user_id, note.id)
        assert note.read_at == when

    def test_unless_unread_avoids_piling_up(self, user_id):
        assert notifications.notify(user_id, Kind.SHARES_NOT_100, "A", unless_unread=True)
        db.session.commit()
        assert notifications.notify(user_id, Kind.SHARES_NOT_100, "B", unless_unread=True) is None
        assert notifications.notify(user_id, Kind.SETTLED, "Other kind", unless_unread=True)
        notifications.mark_all_read(user_id)
        assert notifications.notify(user_id, Kind.SHARES_NOT_100, "C", unless_unread=True)

    def test_someone_elses_are_out_of_reach(self, user_id):
        other = UserFactory()
        note = notifications.notify(other.id, Kind.ANNOUNCEMENT, "Theirs")
        db.session.commit()
        assert notifications.list_for(user_id) == []
        assert notifications.unread_count(user_id) == 0
        with pytest.raises(NotFoundError):
            notifications.mark_read(user_id, note.id)
        assert notifications.mark_all_read(user_id) == 0
        assert notifications.unread_count(other.id) == 1


class TestFlags:
    def test_unknown_flag_is_off(self, user_id):
        assert flags.is_enabled("no-such-flag", user_id) is False

    def test_off_by_default_when_created(self, user_id):
        flags.set_flag("new-thing", actor_id=None, description="A new thing")
        assert flags.is_enabled("new-thing", user_id) is False

    def test_fully_on(self, user_id):
        flags.set_flag("new-thing", actor_id=None, enabled=True, rollout_pct=100)
        assert flags.is_enabled("new-thing", user_id) is True
        assert flags.is_enabled("new-thing") is True, "also for people who aren't logged in"

    def test_enabled_at_zero_percent_only_reaches_the_allowlist(self, user_id):
        other = UserFactory()
        db.session.commit()
        flags.set_flag("new-thing", actor_id=None, enabled=True, allow_user_ids=[user_id])
        assert flags.is_enabled("new-thing", user_id) is True
        assert flags.is_enabled("new-thing", other.id) is False
        assert flags.is_enabled("new-thing") is False

    def test_off_is_a_kill_switch_even_for_the_allowlist(self, user_id):
        flags.set_flag(
            "new-thing", actor_id=None, enabled=False, rollout_pct=100, allow_user_ids=[user_id]
        )
        assert flags.is_enabled("new-thing", user_id) is False

    def test_rollout_covers_about_the_right_share_of_people(self):
        people = range(1, 2001)
        flag = FeatureFlag(key="k", enabled=True, rollout_pct=10, allow_user_ids=[])
        included = sum(flags.flag_applies(flag, person) for person in people)
        assert 140 <= included <= 260

    def test_raising_the_rollout_only_ever_adds_people(self):
        people = range(1, 1001)
        ten = {
            p
            for p in people
            if flags.flag_applies(
                FeatureFlag(key="k", enabled=True, rollout_pct=10, allow_user_ids=[]), p
            )
        }
        fifty = {
            p
            for p in people
            if flags.flag_applies(
                FeatureFlag(key="k", enabled=True, rollout_pct=50, allow_user_ids=[]), p
            )
        }
        assert ten < fifty

    def test_each_flag_picks_different_people(self):
        people = range(1, 501)

        def picks(key):
            flag = FeatureFlag(key=key, enabled=True, rollout_pct=20, allow_user_ids=[])
            return {p for p in people if flags.flag_applies(flag, p)}

        assert picks("one") != picks("two")

    @pytest.mark.parametrize("pct", [-1, 101])
    def test_rejects_a_bad_percentage(self, ctx, pct):
        with pytest.raises(ValidationFailed):
            flags.set_flag("x", actor_id=None, rollout_pct=pct)

    @pytest.mark.parametrize("key", ["", "x" * 65])
    def test_rejects_a_bad_name(self, ctx, key):
        with pytest.raises(ValidationFailed):
            flags.set_flag(key, actor_id=None)

    def test_update_changes_only_what_is_given(self, ctx):
        flags.set_flag("x", actor_id=None, enabled=True, rollout_pct=50, description="Keep me")
        flags.set_flag("x", actor_id=None, rollout_pct=60)
        flag = flags.list_flags()[0]
        assert (flag.enabled, flag.rollout_pct, flag.description) == (True, 60, "Keep me")

    def test_changes_are_audited(self, user_id):
        flags.set_flag("x", actor_id=user_id, enabled=True, rollout_pct=10)
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "flag.changed"))
        assert entry.actor_id == user_id
        assert entry.details["rollout_pct"] == 10
        flags.delete_flag("x", actor_id=user_id)
        assert flags.list_flags() == []
        assert db.session.scalar(select(AuditLog).where(AuditLog.action == "flag.deleted"))

    def test_deleting_a_missing_flag(self, ctx):
        with pytest.raises(NotFoundError):
            flags.delete_flag("nope", actor_id=None)


class TestAudit:
    def test_records_who_what_and_where(self, app: Flask, user_id):
        with app.test_request_context(
            "/somewhere",
            headers={"User-Agent": "Browser/1.0"},
            environ_base={"REMOTE_ADDR": "203.0.113.5"},
        ):
            app.preprocess_request()
            entry = audit.record(
                "thing.done",
                actor_id=user_id,
                target_type="thing",
                target_id=5,
                details={"why": "because"},
            )
            db.session.commit()
            assert str(entry.ip) == "203.0.113.5"
            assert entry.user_agent == "Browser/1.0"
            assert entry.request_id
            assert entry.actor_id == user_id
            assert (entry.target_type, entry.target_id) == ("thing", 5)
            assert entry.details == {"why": "because"}

    def test_works_outside_a_request(self, ctx):
        entry = audit.record("job.ran")
        db.session.commit()
        assert entry.ip is None
        assert entry.user_agent is None
        assert entry.request_id is None

    def test_joins_the_callers_transaction(self, ctx):
        audit.record("rolled.back")
        db.session.rollback()
        assert db.session.scalar(select(AuditLog)) is None

    def test_a_very_long_or_odd_user_agent_is_safe(self, app: Flask, ctx):
        with app.test_request_context("/", headers={"User-Agent": "x" * 1000}):
            entry = audit.record("thing.done")
            db.session.commit()
        assert len(entry.user_agent) == 255

    def test_a_bad_remote_address_does_not_crash(self, app: Flask, ctx):
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "unix:/tmp/gunicorn.sock"}):
            entry = audit.record("thing.done")
            db.session.commit()
        assert entry.ip is None
