"""The notifications page and the bell."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import Notification, User
from app.services import auth, notifications
from app.services.notifications import Kind
from tests.conftest import login
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration


@pytest.fixture
def user_id(app):
    with app.app_context():
        return auth.register("alex", PASSWORD).user.id


@pytest.fixture
def signed_in(client, user_id):
    login(client, "alex", PASSWORD)
    return client


def send(app, user_id, title="Hello", body="Body text", url=None):
    with app.app_context():
        note = notifications.notify(user_id, Kind.ANNOUNCEMENT, title, body, url=url)
        db.session.commit()
        return note.id


class TestPage:
    def test_empty(self, signed_in):
        response = signed_in.get("/app/notifications")
        assert response.status_code == 200
        assert "No notifications yet" in response.text
        assert "Mark all as read" not in response.text

    def test_lists_them(self, app, signed_in, user_id):
        send(app, user_id, "First", "One")
        send(app, user_id, "Second", "Two")
        text = signed_in.get("/app/notifications").text
        assert text.index("Second") < text.index("First")
        assert "Unread" in text
        assert "Mark all as read" in text

    def test_needs_a_login(self, client):
        assert client.get("/app/notifications").status_code == 302
        assert client.post("/app/notifications/read-all").status_code == 302

    def test_unread_tab(self, app, signed_in, user_id):
        note = send(app, user_id, "Already read")
        send(app, user_id, "Still new")
        signed_in.post(f"/app/notifications/{note}/read")
        text = signed_in.get("/app/notifications?show=unread").text
        assert "Still new" in text
        assert "Already read" not in text
        assert "Unread (1)" in text

    def test_nothing_unread(self, app, signed_in, user_id):
        send(app, user_id)
        signed_in.post("/app/notifications/read-all")
        assert "Nothing unread" in signed_in.get("/app/notifications?show=unread").text

    def test_mark_read_stays_where_you_were(self, app, signed_in, user_id):
        note = send(app, user_id)
        response = signed_in.post(
            f"/app/notifications/{note}/read", data={"next": "/app/notifications?show=unread"}
        )
        assert response.headers["Location"] == "/app/notifications?show=unread"
        with app.app_context():
            assert notifications.unread_count(user_id) == 0

    def test_mark_read_wont_send_you_elsewhere(self, app, signed_in, user_id):
        note = send(app, user_id)
        response = signed_in.post(
            f"/app/notifications/{note}/read", data={"next": "https://evil.example/"}
        )
        assert response.headers["Location"] == "/app/notifications"

    def test_mark_all_read(self, app, signed_in, user_id):
        send(app, user_id)
        send(app, user_id)
        response = signed_in.post("/app/notifications/read-all", follow_redirects=True)
        assert "All notifications marked as read" in response.text
        with app.app_context():
            assert notifications.unread_count(user_id) == 0

    def test_opening_marks_it_read_and_goes_there(self, app, signed_in, user_id):
        note = send(app, user_id, url="/app/split")
        response = signed_in.post(f"/app/notifications/{note}/open")
        assert response.headers["Location"] == "/app/split"
        with app.app_context():
            assert notifications.unread_count(user_id) == 0

    def test_opening_never_leaves_the_site(self, app, signed_in, user_id):
        note = send(app, user_id, url="https://evil.example/")
        response = signed_in.post(f"/app/notifications/{note}/open")
        assert response.headers["Location"] == "/app/notifications"

    def test_opening_one_with_no_link(self, app, signed_in, user_id):
        note = send(app, user_id)
        response = signed_in.post(f"/app/notifications/{note}/open")
        assert response.headers["Location"] == "/app/notifications"

    def test_only_offers_view_when_theres_somewhere_to_go(self, app, signed_in, user_id):
        with_link = send(app, user_id, url="/app/split")
        without = send(app, user_id)
        text = signed_in.get("/app/notifications").text
        assert f"/app/notifications/{with_link}/open" in text
        assert f"/app/notifications/{without}/open" not in text

    def test_read_with_no_link_has_no_buttons(self, app, signed_in, user_id):
        note = send(app, user_id)
        signed_in.post(f"/app/notifications/{note}/read")
        assert f"/app/notifications/{note}/" not in signed_in.get("/app/notifications").text

    def test_someone_elses_is_a_404(self, app, signed_in):
        with app.app_context():
            other = auth.register("other", PASSWORD).user.id
        note = send(app, other, "Private")
        assert "Private" not in signed_in.get("/app/notifications").text
        assert signed_in.post(f"/app/notifications/{note}/read").status_code == 404
        assert signed_in.post(f"/app/notifications/{note}/open").status_code == 404
        with app.app_context():
            assert db.session.get(Notification, note).read_at is None

    def test_get_changes_nothing(self, app, signed_in, user_id):
        note = send(app, user_id)
        assert signed_in.get(f"/app/notifications/{note}/read").status_code == 405
        assert signed_in.get(f"/app/notifications/{note}/open").status_code == 405

    def test_what_it_says_is_escaped(self, app, signed_in, user_id):
        send(app, user_id, "<script>alert(1)</script>", "<script>alert(2)</script>")
        text = signed_in.get("/app/notifications").text
        assert "<script>alert(1)" not in text
        assert "<script>alert(2)" not in text

    def test_paging(self, app, signed_in, user_id):
        for number in range(30):
            send(app, user_id, f"Note {number}")
        assert "Page 1 of 2" in signed_in.get("/app/notifications").text
        assert "Page 2 of 2" in signed_in.get("/app/notifications?page=2").text
        assert "Page 2 of 2" in signed_in.get("/app/notifications?page=9").text
        assert "Page 1 of 2" in signed_in.get("/app/notifications?page=abc").text


class TestBell:
    def test_shows_the_count_on_every_app_page(self, app, signed_in, user_id):
        send(app, user_id)
        send(app, user_id)
        for path in ("/app", "/app/sales", "/app/settings"):
            text = signed_in.get(path).text
            assert "Notifications, 2 unread" in text, path
            assert '<span class="bell-count" aria-hidden="true">2</span>' in text, path

    def test_no_number_when_nothing_is_unread(self, signed_in):
        text = signed_in.get("/app").text
        assert '<span class="sr-only">Notifications</span>' in text
        assert 'class="bell-count"' not in text

    def test_a_big_number_stays_small(self, app, signed_in, user_id):
        with app.app_context():
            for _ in range(120):
                notifications.notify(user_id, Kind.ANNOUNCEMENT, "x")
            db.session.commit()
        assert ">99+<" in signed_in.get("/app").text

    def test_the_count_can_be_refreshed_on_its_own(self, app, signed_in, user_id):
        send(app, user_id)
        response = signed_in.get("/app/notifications/bell")
        assert response.status_code == 200
        assert "1 unread" in response.text
        assert "<html" not in response.text
        assert 'hx-trigger="every 120s"' in response.text

    def test_after_logging_out_it_changes_nothing(self, client, user_id):
        # A redirect would put the login page into the top band.
        assert client.get("/app/notifications/bell").status_code == 204

    def test_checking_for_notifications_doesnt_extend_a_session(self, signed_in):
        """A tab left open on a shared computer has to log out eventually."""
        assert "Set-Cookie" in signed_in.get("/app").headers
        assert "Set-Cookie" not in signed_in.get("/app/notifications/bell").headers

    def test_works_with_a_temporary_password(self, app, signed_in, user_id):
        with app.app_context():
            db.session.get(User, user_id).must_change_password = True
            db.session.commit()
        assert signed_in.get("/app/notifications/bell").status_code == 200

    def test_error_pages_dont_ask_the_database(self, signed_in):
        assert "bell-count" not in signed_in.get("/no-such-page").text


def test_notification_rows_belong_to_the_account(app, user_id):
    with app.app_context():
        assert not db.session.scalars(
            select(Notification).where(Notification.user_id != user_id)
        ).all()
