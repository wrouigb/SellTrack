"""The admin console, through its real routes."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from flask import url_for
from sqlalchemy import func, select

from app.extensions import db
from app.models import (
    AuditLog,
    FeatureFlag,
    Notification,
    SupportMessage,
    SupportStatus,
    User,
    UserRole,
    UserStatus,
)
from app.services import audit, auth, flags, notifications, products, sales, split, support, system
from app.services.notifications import Kind
from tests.conftest import csrf_token_from, login
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "a-long-admin-passphrase"
DAY = "2026-11-05"
XSS = "<script>alert(1)</script>"


@pytest.fixture
def admin_id(app):
    with app.app_context():
        return auth.create_admin("chief", ADMIN_PASSWORD).id


@pytest.fixture
def admin(client, admin_id):
    """A client logged in as the admin."""
    assert login(client, "chief", ADMIN_PASSWORD).status_code == 302
    return client


@pytest.fixture
def member(app):
    with app.app_context():
        return auth.register("alex", PASSWORD).user.id


def audit_entries(action):
    return db.session.scalars(select(AuditLog).where(AuditLog.action == action)).all()


def fill_account(user_id):
    """An account with records the admin has no business seeing, one of everything."""
    product = products.create_product(user_id, name="SECRET-PRODUCT", cost="11.11", sell_price="22")
    alex = split.add_person(user_id, name="SECRET-PERSON-A", share_pct="60")
    sam = split.add_person(user_id, name="SECRET-PERSON-B", share_pct="40")
    sales.create_sale(
        user_id,
        product_id=product.id,
        sale_date=DAY,
        quantity=1,
        price_paid="987.65",
        customer_name="SECRET-CUSTOMER",
        notes="SECRET-NOTE",
        received_by_partner_id=sam.id,
    )
    split.add_cost(
        user_id,
        description="SECRET-COST",
        category="stock",
        amount="43.21",
        cost_date=DAY,
        paid_by_partner_id=alex.id,
    )
    split.record_payment(
        user_id,
        from_partner_id=sam.id,
        to_partner_id=alex.id,
        amount="12.34",
        paid_on=DAY,
        note="SECRET-PAYMENT",
    )


class TestAccess:
    """Every admin route, found from the app itself, so a new one can't be missed."""

    @staticmethod
    def _routes(app):
        with app.test_request_context():
            for rule in app.url_map.iter_rules():
                if rule.endpoint.startswith("admin."):
                    ids = dict.fromkeys(rule.arguments, 1)
                    for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                        yield method, url_for(rule.endpoint, **ids)

    def test_there_are_routes_to_check(self, app):
        assert len(list(self._routes(app))) > 15

    def test_guests_go_to_login(self, app, client):
        for method, path in self._routes(app):
            response = client.open(path, method=method)
            assert response.status_code == 302, (method, path)
            assert response.headers["Location"].startswith("/login"), (method, path)

    def test_normal_users_get_403(self, app, client, member):
        login(client, "alex", PASSWORD)
        for method, path in self._routes(app):
            assert client.open(path, method=method).status_code == 403, (method, path)

    def test_a_normal_user_changes_nothing(self, app, client, member, admin_id):
        login(client, "alex", PASSWORD)
        client.post(f"/admin/users/{admin_id}/suspend")
        client.post("/admin/users/bulk-suspend", data={"ids": [admin_id]})
        client.post(f"/admin/users/{admin_id}/reset-password")
        client.post("/admin/announcements", data={"title": "Hi", "body": "There"})
        with app.app_context():
            chief = db.session.get(User, admin_id)
            assert chief.status is UserStatus.ACTIVE
            assert not chief.must_change_password
            assert db.session.scalar(select(func.count()).select_from(Notification)) == 0

    def test_admins_reach_every_page(self, app, admin):
        for method, path in self._routes(app):
            if method == "GET":
                # An ID that doesn't exist is a 404, but never a 403 or a crash.
                assert admin.get(path).status_code in (200, 404), path

    def test_every_form_needs_a_csrf_token(self, csrf_client):
        with csrf_client.application.app_context():
            auth.create_admin("chief", ADMIN_PASSWORD)
        page = csrf_client.get("/login")
        csrf_client.post(
            "/login",
            data={
                "username": "chief",
                "password": ADMIN_PASSWORD,
                "csrf_token": csrf_token_from(page.text),
            },
        )
        assert csrf_client.get("/admin").status_code == 200
        response = csrf_client.post(
            "/admin/announcements", data={"title": "Forged", "body": "Forged"}
        )
        assert response.status_code == 400

    def test_a_temporary_password_comes_before_the_console(self, app, admin, admin_id):
        with app.app_context():
            db.session.get(User, admin_id).must_change_password = True
            db.session.commit()
        response = admin.get("/admin/users")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/app/settings/security/password")

    def test_the_menu_only_offers_admin_to_admins(self, client, app, member, admin_id):
        login(client, "alex", PASSWORD)
        assert 'href="/admin"' not in client.get("/app").text
        client.post("/logout")
        login(client, "chief", ADMIN_PASSWORD)
        assert 'href="/admin"' in client.get("/app").text


class TestPrivacy:
    """The admin sees account details and counts, never what's in an account."""

    def test_nothing_from_inside_an_account_reaches_any_admin_page(self, app, admin, member):
        with app.app_context():
            fill_account(member)
            split.settle_up(member, actor_id=member)
            split.undo_last_settlement(member, actor_id=member)
            split.settle_up(member, actor_id=member)

        pages = [
            "/admin",
            "/admin/users",
            f"/admin/users/{member}",
            "/admin/support?status=all",
            "/admin/announcements",
            "/admin/audit",
            "/admin/flags",
            "/admin/system",
        ]
        for path in pages:
            text = admin.get(path).text
            assert "SECRET" not in text, path
            for amount in ("987.65", "43.21", "12.34", "11.11", "932.31"):
                assert amount not in text, (path, amount)

    def test_the_audit_log_itself_holds_no_names_or_money(self, app, member):
        """It outlives the account, so this is what keeps deletion honest."""
        with app.app_context():
            fill_account(member)
            split.settle_up(member, actor_id=member)
            split.undo_last_settlement(member, actor_id=member)
            for entry in db.session.scalars(select(AuditLog)):
                text = str(entry.details)
                assert "SECRET" not in text, entry.action
                assert "987.65" not in text, entry.action

    def test_details_not_on_the_list_stay_hidden(self, app, admin, admin_id):
        with app.app_context():
            audit.record("split.settled", details={"sales": 3, "money_in": "555.55"})
            audit.record("something.new", details={"surprise": "HIDDEN-VALUE"})
            db.session.commit()
        text = admin.get("/admin/audit").text
        assert "sales: 3" in text
        assert "555.55" not in text
        assert "HIDDEN-VALUE" not in text
        assert "something.new" in text

    def test_the_counts_are_right(self, app, admin, member):
        with app.app_context():
            fill_account(member)
        text = admin.get(f"/admin/users/{member}").text
        for label, count in (
            ("Sales", 1),
            ("Products", 1),
            ("People in the split", 2),
            ("Upfront costs", 1),
            ("Payments", 1),
            ("Settle-ups", 0),
        ):
            assert re.search(rf"{label}</span><span class=\"num\">{count}<", text), label

    def test_every_action_recorded_anywhere_has_a_label(self):
        """An action without one would show up to the admin as a code."""
        recorded = set()
        for path in Path("app").rglob("*.py"):
            recorded |= set(re.findall(r'audit\.record\(\s*"([a-z_.]+)"', path.read_text()))
        recorded |= set(
            re.findall(r'action="([a-z_.]+)"', Path("app/services/auth.py").read_text())
        )
        assert len(recorded) > 15
        assert recorded <= set(audit.ACTION_LABELS)


class TestUserList:
    @pytest.fixture
    def several(self, app, admin_id):
        with app.app_context():
            for name in ("alice", "bob", "carol", "dash_name"):
                auth.register(name, PASSWORD)
            carol = db.session.scalar(select(User).where(User.username == "carol"))
            carol.status = UserStatus.SUSPENDED
            bob = db.session.scalar(select(User).where(User.username == "bob"))
            bob.last_login_at = datetime(2026, 1, 1, tzinfo=UTC)
            db.session.commit()

    def names(self, response):
        return re.findall(r'href="/admin/users/\d+">([^<]+)<', response.text)

    def test_lists_everyone_newest_first(self, admin, several):
        assert self.names(admin.get("/admin/users")) == [
            "dash_name",
            "carol",
            "bob",
            "alice",
            "chief",
        ]

    def test_search_is_literal_and_ignores_case(self, admin, several):
        assert self.names(admin.get("/admin/users?q=ALI")) == ["alice"]
        assert self.names(admin.get("/admin/users?q=%25")) == []
        assert self.names(admin.get("/admin/users?q=_")) == ["dash_name"]

    def test_filters(self, admin, several):
        assert self.names(admin.get("/admin/users?status=suspended")) == ["carol"]
        assert self.names(admin.get("/admin/users?role=admin")) == ["chief"]
        assert "No accounts match" in admin.get("/admin/users?q=zzz").text

    def test_sorting(self, admin, several):
        ascending = self.names(admin.get("/admin/users?sort=username&dir=asc"))
        assert ascending == ["alice", "bob", "carol", "chief", "dash_name"]
        # Someone who has never logged in sorts last either way, rather than first.
        latest = self.names(admin.get("/admin/users?sort=last_login&dir=desc"))
        assert latest[0] == "chief"
        assert latest[1] == "bob"

    def test_a_bad_address_is_just_ignored(self, admin, several):
        for query in ("sort=password_hash", "status=nonsense", "role=nonsense", "page=abc"):
            response = admin.get(f"/admin/users?{query}")
            assert response.status_code == 200, query
            assert len(self.names(response)) == 5, query

    def test_paging(self, app, admin, admin_id):
        with app.app_context():
            for number in range(30):
                db.session.add(User(username=f"user{number:02d}", password_hash="x"))
            db.session.commit()
        first = admin.get("/admin/users")
        assert len(self.names(first)) == 25
        assert "Page 1 of 2" in first.text
        second = admin.get("/admin/users?page=2")
        assert len(self.names(second)) == 6
        assert "Page 2 of 2" in admin.get("/admin/users?page=99").text

    def test_nobody_can_tick_themselves(self, admin, admin_id, member):
        text = admin.get("/admin/users").text
        assert f'value="{member}"' in text
        assert f'value="{admin_id}"' not in text


class TestSuspending:
    def test_bulk_suspend(self, app, admin, admin_id):
        with app.app_context():
            ids = [auth.register(name, PASSWORD).user.id for name in ("one1", "two2", "three3")]
        response = admin.post(
            "/admin/users/bulk-suspend",
            data={"ids": [ids[0], ids[1], admin_id], "next": "/admin/users?q=o"},
        )
        assert response.headers["Location"] == "/admin/users?q=o"
        with app.app_context():
            statuses = {
                user.username: user.status for user in db.session.scalars(select(User)).all()
            }
            assert statuses["one1"] is UserStatus.SUSPENDED
            assert statuses["two2"] is UserStatus.SUSPENDED
            assert statuses["three3"] is UserStatus.ACTIVE
            assert statuses["chief"] is UserStatus.ACTIVE
            entries = audit_entries("admin.user_status_changed")
            assert {entry.target_id for entry in entries} == {ids[0], ids[1]}
            assert all(entry.actor_id == admin_id for entry in entries)

    def test_bulk_suspend_says_when_nothing_happened(self, admin, admin_id):
        response = admin.post(
            "/admin/users/bulk-suspend", data={"ids": [admin_id]}, follow_redirects=True
        )
        assert "Nothing to suspend" in response.text

    def test_bulk_suspend_ignores_rubbish_and_a_bad_return_address(self, admin, member):
        response = admin.post(
            "/admin/users/bulk-suspend",
            data={"ids": ["abc", "", "-1", str(10**30)], "next": "https://evil.example/"},
        )
        assert response.headers["Location"] == "/admin/users"

    def test_suspending_twice_is_recorded_once(self, app, admin, member):
        admin.post(f"/admin/users/{member}/suspend")
        admin.post(f"/admin/users/{member}/suspend")
        admin.post("/admin/users/bulk-suspend", data={"ids": [member]})
        with app.app_context():
            assert len(audit_entries("admin.user_status_changed")) == 1

    def test_suspending_ends_their_session_for_good(self, app, admin, member):
        other = app.test_client()
        login(other, "alex", PASSWORD)
        assert other.get("/app").status_code == 200

        admin.post(f"/admin/users/{member}/suspend")
        assert other.get("/app").status_code == 302

        # Coming back doesn't revive the old session, which might be someone else's stolen one.
        admin.post(f"/admin/users/{member}/reactivate")
        assert other.get("/app").status_code == 302
        fresh = app.test_client()
        login(fresh, "alex", PASSWORD)
        assert fresh.get("/app").status_code == 200

    def test_admin_cant_suspend_themselves(self, app, admin, admin_id):
        response = admin.post(f"/admin/users/{admin_id}/suspend", follow_redirects=True)
        assert "can&#39;t suspend your own account" in response.text
        with app.app_context():
            assert db.session.get(User, admin_id).status is UserStatus.ACTIVE

    def test_someone_who_doesnt_exist(self, admin):
        assert admin.post("/admin/users/999999/suspend").status_code == 404
        assert admin.get("/admin/users/999999").status_code == 404


class TestPasswordReset:
    TEMPORARY = re.compile(r"<code>([\w-]+)</code>")

    def test_reset_shows_it_once_and_it_works(self, app, admin, admin_id, member):
        response = admin.post(f"/admin/users/{member}/reset-password")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        temporary = self.TEMPORARY.search(response.text).group(1)

        # It isn't kept anywhere the admin could go back to, including the session cookie.
        with admin.session_transaction() as session:
            assert temporary not in str(dict(session))
        assert temporary not in admin.get(f"/admin/users/{member}").text
        assert temporary not in admin.get("/admin/audit").text
        with app.app_context():
            assert temporary not in str(audit_entries("admin.password_reset")[0].details)
            assert audit_entries("admin.password_reset")[0].actor_id == admin_id

        client = app.test_client()
        assert login(client, "alex", PASSWORD).status_code == 401
        assert login(client, "alex", temporary).status_code == 302
        forced = client.get("/app/sales")
        assert forced.headers["Location"].endswith("/app/settings/security/password")

    def test_old_sessions_end(self, app, admin, member):
        other = app.test_client()
        login(other, "alex", PASSWORD)
        admin.post(f"/admin/users/{member}/reset-password")
        assert other.get("/app").status_code == 302

    def test_they_are_told(self, app, admin, member):
        admin.post(f"/admin/users/{member}/reset-password")
        with app.app_context():
            kinds = [n.kind for n in notifications.list_for(member)]
            assert Kind.PASSWORD_RESET_BY_ADMIN.value in kinds

    def test_not_your_own(self, app, admin, admin_id):
        response = admin.post(f"/admin/users/{admin_id}/reset-password", follow_redirects=True)
        assert "Change your own password under Settings" in response.text
        with app.app_context():
            assert not db.session.get(User, admin_id).must_change_password
            assert not audit_entries("admin.password_reset")


class TestSupportInbox:
    @pytest.fixture
    def message(self, app, member):
        with app.app_context():
            return support.send_message(member, subject="Help me", body="It broke").id

    def test_waiting_messages_show_first_oldest_at_the_top(self, app, admin, member, message):
        with app.app_context():
            support.send_message(member, subject="Second", body="More")
        text = admin.get("/admin/support").text
        assert text.index("Help me") < text.index("Second")
        assert "alex" in text
        assert "Support (2)" in text

    def test_reply(self, app, admin, admin_id, member, message):
        response = admin.post(f"/admin/support/{message}", data={"reply": "Try this"})
        assert response.status_code == 302
        with app.app_context():
            saved = db.session.get(SupportMessage, message)
            assert saved.status is SupportStatus.ANSWERED
            assert saved.admin_reply == "Try this"
            assert saved.replied_at is not None
            note = notifications.list_for(member)[0]
            assert note.kind == Kind.SUPPORT_REPLY.value
            assert note.title == "Support replied: Help me"
            assert note.body == "Try this"
            assert note.url == f"/app/support#message-{message}"
            entry = audit_entries("support.replied")[0]
            assert (entry.actor_id, entry.target_id) == (admin_id, message)
            assert "Try this" not in str(entry.details)
        assert "Help me" not in admin.get("/admin/support").text
        assert "Help me" in admin.get("/admin/support?status=answered").text

    def test_an_empty_reply_is_refused(self, app, admin, message):
        response = admin.post(f"/admin/support/{message}", data={"reply": "   "})
        assert response.status_code == 422
        assert "The reply can&#39;t be empty" in response.text
        with app.app_context():
            assert db.session.get(SupportMessage, message).status is SupportStatus.OPEN

    def test_changing_a_reply_tells_them(self, app, admin, member, message):
        admin.post(f"/admin/support/{message}", data={"reply": "First"})
        assert "First" in admin.get(f"/admin/support/{message}").text
        admin.post(f"/admin/support/{message}", data={"reply": "Second"})
        with app.app_context():
            titles = [n.title for n in notifications.list_for(member)]
            assert titles[0] == "Support changed its reply: Help me"
            assert db.session.get(SupportMessage, message).admin_reply == "Second"

    def test_closing_and_reopening(self, app, admin, message):
        admin.post(f"/admin/support/{message}/close")
        assert "Help me" not in admin.get("/admin/support").text
        assert "Help me" in admin.get("/admin/support?status=closed").text
        page = admin.get(f"/admin/support/{message}")
        assert "Reopen" in page.text
        refused = admin.post(f"/admin/support/{message}", data={"reply": "Too late"})
        assert refused.status_code == 422
        assert "Reopen it to reply" in refused.text

        admin.post(f"/admin/support/{message}/reopen")
        with app.app_context():
            assert db.session.get(SupportMessage, message).status is SupportStatus.OPEN
            assert audit_entries("support.closed")
            assert audit_entries("support.reopened")

    def test_reopening_an_answered_message_goes_back_to_answered(self, app, admin, message):
        admin.post(f"/admin/support/{message}", data={"reply": "Done"})
        admin.post(f"/admin/support/{message}/close")
        admin.post(f"/admin/support/{message}/reopen")
        with app.app_context():
            assert db.session.get(SupportMessage, message).status is SupportStatus.ANSWERED

    def test_search(self, admin, message):
        assert "Help me" in admin.get("/admin/support?q=HELP").text
        assert "Help me" in admin.get("/admin/support?q=ale").text
        assert "Help me" not in admin.get("/admin/support?q=zzz").text
        assert "No messages match" in admin.get("/admin/support?q=zzz").text

    def test_what_people_write_is_escaped(self, app, admin, member):
        with app.app_context():
            message_id = support.send_message(member, subject=XSS, body=XSS).id
        for path in ("/admin/support", f"/admin/support/{message_id}"):
            assert XSS not in admin.get(path).text

    def test_a_message_that_doesnt_exist(self, admin):
        assert admin.get("/admin/support/999").status_code == 404
        assert admin.post("/admin/support/999", data={"reply": "x"}).status_code == 404
        assert admin.post("/admin/support/999/close").status_code == 404
        assert admin.post("/admin/support/999/reopen").status_code == 404

    def test_a_deleted_account_takes_its_messages_with_it(self, app, admin, member, message):
        with app.app_context():
            user = db.session.get(User, member)
            auth.delete_account(user, PASSWORD)
        assert admin.get(f"/admin/support/{message}").status_code == 404


class TestAnnouncements:
    def test_goes_to_active_accounts_only(self, app, admin, admin_id, member):
        with app.app_context():
            away = auth.register("away", PASSWORD).user.id
            db.session.get(User, away).status = UserStatus.SUSPENDED
            db.session.commit()
        response = admin.post(
            "/admin/announcements",
            data={"title": "Maintenance", "body": "Saturday", "url": "/help"},
            follow_redirects=True,
        )
        assert "Announcement sent to 2 accounts." in response.text
        with app.app_context():
            for user_id in (admin_id, member):
                note = notifications.list_for(user_id)[0]
                assert note.kind == Kind.ANNOUNCEMENT.value
                assert (note.title, note.body, note.url) == ("Maintenance", "Saturday", "/help")
            assert not [
                n for n in notifications.list_for(away) if n.kind == Kind.ANNOUNCEMENT.value
            ]
            entry = audit_entries("announcement.sent")[0]
            assert entry.actor_id == admin_id
            assert entry.details["recipients"] == 2

    def test_past_ones_are_listed(self, admin, member):
        admin.post("/admin/announcements", data={"title": "First news", "body": "Details here"})
        text = admin.get("/admin/announcements").text
        assert "First news" in text
        assert "Details here" in text

    @pytest.mark.parametrize(
        "url",
        ["https://evil.example/", "//evil.example", "/\t/evil.example", "javascript:alert(1)"],
    )
    def test_a_link_has_to_stay_on_this_site(self, app, admin, member, url):
        response = admin.post(
            "/admin/announcements", data={"title": "Hi", "body": "There", "url": url}
        )
        assert response.status_code == 422
        assert "Use a path on this site" in response.text
        with app.app_context():
            assert notifications.unread_count(member) == 0

    @pytest.mark.parametrize(
        ("title", "body", "message"),
        [
            ("", "Body", "The title can&#39;t be empty"),
            ("Title", "", "The message can&#39;t be empty"),
            ("x" * 201, "Body", "Use at most 200 characters"),
            ("Title", "x" * 2001, "Use at most 2000 characters"),
        ],
    )
    def test_checks_what_it_sends(self, admin, title, body, message):
        response = admin.post("/admin/announcements", data={"title": title, "body": body})
        assert response.status_code == 422
        assert message in response.text

    def test_a_title_can_be_as_long_as_a_notification_allows(self, app, admin, member):
        admin.post("/admin/announcements", data={"title": "x" * 200, "body": "Body"})
        with app.app_context():
            assert len(notifications.list_for(member)[0].title) == 200


class TestAuditLogPage:
    @pytest.fixture
    def entries(self, app, admin_id, member):
        with app.app_context():
            audit.record("auth.login", actor_id=member, target_type="user", target_id=member)
            audit.record("flag.deleted", actor_id=admin_id, details={"key": "old-flag"})
            db.session.commit()

    def test_shows_who_did_what_in_words(self, admin, entries):
        text = admin.get("/admin/audit").text
        assert "Logged in" in text
        assert "Feature flag deleted" in text
        assert "key: old-flag" in text

    def test_filter_by_action(self, admin, entries):
        text = admin.get("/admin/audit?action=flag.deleted").text
        assert "old-flag" in text
        assert "1 entry" in text

    def test_filter_by_who(self, admin, entries):
        assert "old-flag" not in admin.get("/admin/audit?actor=alex").text
        assert "old-flag" in admin.get("/admin/audit?actor=CHIEF").text
        assert "Nothing matches" in admin.get("/admin/audit?actor=nobody").text

    def test_filter_by_date_uses_the_admins_time_zone(self, app, admin, admin_id, entries):
        moment = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)  # 1am on 2 March in Melbourne
        with app.app_context():
            entry = audit.record("flag.deleted", actor_id=admin_id, details={"key": "dated"})
            entry.created_at = moment
            db.session.commit()
        assert "dated" in admin.get("/admin/audit?date_from=2026-03-02&date_to=2026-03-02").text
        assert "dated" not in admin.get("/admin/audit?date_from=2026-03-01&date_to=2026-03-01").text

    @pytest.mark.parametrize(
        "query",
        [
            "date_from=nonsense",
            "date_to=9999-12-31",
            "date_from=0001-01-01",
            "date_to=2026-13-45",
            "action=nonsense",
            "page=abc",
            "actor=" + "x" * 5000,
        ],
    )
    def test_a_bad_address_doesnt_break_it(self, admin, entries, query):
        assert admin.get(f"/admin/audit?{query}").status_code == 200

    def test_someone_deleted_is_described_as_gone(self, app, admin, member, entries):
        with app.app_context():
            auth.delete_account(db.session.get(User, member), PASSWORD)
        text = admin.get("/admin/audit?action=auth.account_deleted").text
        assert "Deleted account" in text
        assert "alex" not in text

    def test_details_are_in_words(self, app, admin, admin_id):
        with app.app_context():
            flags.set_flag("worded", actor_id=admin_id, enabled=True, allow_user_ids=[admin_id])
            flags.set_flag("other", actor_id=admin_id, enabled=False)
        text = admin.get("/admin/audit?action=flag.changed").text
        assert "enabled: Yes" in text
        assert "enabled: No" in text
        assert f"allow user ids: {admin_id}<" in text
        assert "allow user ids: None" in text
        assert "True" not in text
        assert "[]" not in text

    def test_says_who_did_it_when_nobody_did(self, app, admin):
        with app.app_context():
            audit.record("system.cleanup", details={"sales": 0})
            audit.record("auth.login_failed", target_type="user")
            db.session.commit()
        text = admin.get("/admin/audit").text
        assert "SellTrack</td>" in text
        assert "No account" in text

    def test_a_row_with_nothing_in_a_column_leaves_it_truly_empty(self, app, admin, admin_id):
        """Otherwise a phone shows the column's label with nothing beside it."""
        with app.app_context():
            audit.record("auth.logout", actor_id=admin_id)
            db.session.commit()
        text = admin.get("/admin/audit?action=auth.logout").text
        assert '<td role="cell" data-label="To"></td>' in text
        assert '<td role="cell" data-label="Details"></td>' in text

    def test_links_to_who_it_was(self, admin, entries, member):
        assert f'href="/admin/users/{member}"' in admin.get("/admin/audit").text

    def test_paging(self, app, admin, admin_id):
        with app.app_context():
            for _ in range(30):
                audit.record("auth.logout", actor_id=admin_id)
            db.session.commit()
        assert "Page 1 of 2" in admin.get("/admin/audit").text


class TestFlagPages:
    def test_add_starts_switched_off(self, app, admin, admin_id):
        response = admin.post(
            "/admin/flags", data={"key": "new-reports", "description": "New reports"}
        )
        assert response.status_code == 302
        with app.app_context():
            flag = db.session.scalar(select(FeatureFlag))
            assert (flag.key, flag.enabled, flag.rollout_pct) == ("new-reports", False, 0)
            entry = audit_entries("flag.changed")[0]
            assert entry.actor_id == admin_id

    @pytest.mark.parametrize("key", ["", "Has Spaces", "UPPER", "-leading", "x" * 65, "<b>"])
    def test_bad_names_are_refused(self, app, admin, key):
        response = admin.post("/admin/flags", data={"key": key})
        assert response.status_code == 422
        with app.app_context():
            assert not flags.list_flags()

    def test_adding_one_that_exists_leaves_it_alone(self, app, admin, admin_id):
        with app.app_context():
            flags.set_flag("live", actor_id=admin_id, enabled=True, rollout_pct=50)
        response = admin.post("/admin/flags", data={"key": "live"})
        assert response.status_code == 422
        assert "already a flag" in response.text
        with app.app_context():
            flag = flags.list_flags()[0]
            assert (flag.enabled, flag.rollout_pct) == (True, 50)

    def test_roll_out_to_ten_percent(self, app, admin, admin_id, member):
        with app.app_context():
            flag_id = flags.set_flag("slow", actor_id=admin_id).id
        response = admin.post(
            f"/admin/flags/{flag_id}",
            data={
                "enabled": "y",
                "rollout_pct": "10",
                "allow_users": "ALEX, chief",
                "description": "D",
            },
        )
        assert response.status_code == 302
        with app.app_context():
            flag = db.session.get(FeatureFlag, flag_id)
            assert flag.enabled
            assert flag.rollout_pct == 10
            assert flag.allow_user_ids == sorted([admin_id, member])
            assert flags.is_enabled("slow", member)
        page = admin.get(f"/admin/flags/{flag_id}").text
        assert "alex, chief" in page
        assert "alex, chief" in admin.get("/admin/flags").text

    def test_unticking_switches_it_off(self, app, admin, admin_id):
        with app.app_context():
            flag_id = flags.set_flag("on", actor_id=admin_id, enabled=True, rollout_pct=100).id
        admin.post(f"/admin/flags/{flag_id}", data={"rollout_pct": "100"})
        with app.app_context():
            assert not db.session.get(FeatureFlag, flag_id).enabled

    @pytest.mark.parametrize("value", ["", "101", "-1", "abc", "10.5", "٣", "9" * 50])
    def test_a_bad_percentage_is_refused(self, app, admin, admin_id, value):
        with app.app_context():
            flag_id = flags.set_flag("pct", actor_id=admin_id, rollout_pct=5).id
        response = admin.post(f"/admin/flags/{flag_id}", data={"rollout_pct": value})
        assert response.status_code == 422
        assert "percentage from 0 to 100" in response.text
        with app.app_context():
            assert db.session.get(FeatureFlag, flag_id).rollout_pct == 5

    def test_an_unknown_username_is_refused(self, app, admin, admin_id):
        with app.app_context():
            flag_id = flags.set_flag("named", actor_id=admin_id).id
        response = admin.post(
            f"/admin/flags/{flag_id}", data={"rollout_pct": "0", "allow_users": "ghost"}
        )
        assert response.status_code == 422
        assert "No account called ghost" in response.text

    def test_delete(self, app, admin, admin_id):
        with app.app_context():
            flag_id = flags.set_flag("gone", actor_id=admin_id).id
        admin.post(f"/admin/flags/{flag_id}/delete")
        with app.app_context():
            assert not flags.list_flags()
            assert audit_entries("flag.deleted")[0].details == {"key": "gone"}
        assert admin.get(f"/admin/flags/{flag_id}").status_code == 404

    def test_an_allowlisted_account_thats_deleted_is_dropped_from_the_names(
        self, app, admin_id, member
    ):
        with app.app_context():
            flag = flags.set_flag("x", actor_id=admin_id, allow_user_ids=[member, admin_id])
            auth.delete_account(db.session.get(User, member), PASSWORD)
            assert flags.allowlist_names(flag) == ["chief"]


class TestOverviewAndSystem:
    NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

    def test_overview_figures(self, app, admin, member):
        with app.app_context():
            suspended = auth.register("away", PASSWORD).user
            suspended.status = UserStatus.SUSPENDED
            db.session.commit()
            support.send_message(member, subject="Hi", body="There")
            figures = system_figures()
            assert figures.accounts == 3
            assert figures.suspended == 1
            assert figures.open_support == 1
            assert figures.active_recently == 1  # only the admin has logged in
        assert admin.get("/admin").status_code == 200

    def test_backup_not_set_up(self):
        assert system.last_backup(None) == system.Backup(configured=False)

    def test_backup_folder_missing_or_empty(self, tmp_path):
        assert system.last_backup(tmp_path / "nowhere").is_late
        empty = system.last_backup(tmp_path)
        assert empty.configured
        assert empty.is_late
        assert empty.last_at is None

    def test_newest_backup_counts(self, tmp_path):
        import os

        old = tmp_path / "old.dump"
        new = tmp_path / "new.dump"
        old.write_bytes(b"x" * 10)
        new.write_bytes(b"x" * 20)
        (tmp_path / "subfolder").mkdir()
        (tmp_path / "link").symlink_to(old)
        two_hours_ago = (self.NOW - timedelta(hours=2)).timestamp()
        os.utime(old, (0, 0))
        os.utime(new, (two_hours_ago, two_hours_ago))
        backup = system.last_backup(tmp_path, now=self.NOW)
        assert backup.size_bytes == 20
        assert not backup.is_late
        assert backup.last_at == self.NOW - timedelta(hours=2)

    def test_a_backup_over_26_hours_old_is_late(self, tmp_path):
        import os

        path = tmp_path / "b.dump"
        path.write_bytes(b"x")
        for hours, late in ((25, False), (27, True)):
            stamp = (self.NOW - timedelta(hours=hours)).timestamp()
            os.utime(path, (stamp, stamp))
            assert system.last_backup(tmp_path, now=self.NOW).is_late is late

    def test_disk(self, tmp_path):
        disk = system.disk_usage(tmp_path)
        assert disk.total_bytes > 0
        assert system.disk_usage(tmp_path / "nowhere") is None
        assert system.Disk("/", 100, 15).is_low
        assert not system.Disk("/", 100, 16).is_low
        assert system.Disk("/", 0, 0).used_percent == 0

    def test_page_says_what_needs_attention(self, app, admin, monkeypatch, tmp_path):
        text = admin.get("/admin/system").text
        assert "Set BACKUP_DIR" in text
        assert "Hasn't run yet" in text
        assert "Something below needs a look" in text

        monkeypatch.setitem(app.config, "BACKUP_DIR", tmp_path)
        assert "None found" in admin.get("/admin/system").text
        (tmp_path / "b.dump").write_bytes(b"x" * 2000)
        assert "On time" in admin.get("/admin/system").text

        monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: _Usage(100, 5))
        assert "Getting full" in admin.get("/admin/system").text

    def test_page_when_redis_is_down(self, app, admin, monkeypatch):
        monkeypatch.setitem(app.config, "REDIS_URL", "redis://127.0.0.1:1/0")
        assert "Not responding" in admin.get("/admin/system").text

    def test_page_when_redis_isnt_set_up(self, app, admin, monkeypatch):
        monkeypatch.setitem(app.config, "REDIS_URL", None)
        assert "Not set up" in admin.get("/admin/system").text

    def test_all_clear(self, app, admin, monkeypatch, tmp_path):
        from app.jobs.cleanup import run_cleanup

        monkeypatch.setitem(app.config, "BACKUP_DIR", tmp_path)
        (tmp_path / "b.dump").write_bytes(b"x")
        monkeypatch.setattr(system.shutil, "disk_usage", lambda _path: _Usage(100, 50))
        with app.app_context():
            run_cleanup()
        assert "needs a look" not in admin.get("/admin/system").text

    def test_health_copes_with_no_disk_and_no_database(self, app, monkeypatch):
        monkeypatch.setattr(system, "disk_usage", lambda _path: None)
        monkeypatch.setattr(system, "database_ok", lambda: False)
        with app.app_context():
            health = system.health()
            assert health.needs_attention
            assert health.database_bytes is None
            assert health.last_cleanup is None


class _Usage:
    def __init__(self, total, free):
        self.total = total
        self.free = free


def system_figures():
    from app.services import admin as admin_service

    return admin_service.overview()


def test_create_admin_role(app):
    with app.app_context():
        assert auth.create_admin("second", ADMIN_PASSWORD).role is UserRole.ADMIN
