"""Contacting support, from the person's side."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import SupportMessage, SupportStatus
from app.services import auth, support
from app.services.errors import ValidationFailed
from tests.conftest import login
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "a-long-admin-passphrase"


@pytest.fixture
def user_id(app):
    with app.app_context():
        return auth.register("alex", PASSWORD).user.id


@pytest.fixture
def signed_in(client, user_id):
    login(client, "alex", PASSWORD)
    return client


class TestHelpPage:
    def test_guests_are_asked_to_log_in(self, client):
        response = client.get("/help")
        assert 'id="contact"' in response.text
        assert 'name="subject"' not in response.text
        assert "/login?next=/help%23contact" in response.text

    def test_guests_get_no_cookie(self, client):
        assert "Set-Cookie" not in client.get("/help").headers

    def test_logged_in_people_get_the_form(self, signed_in):
        text = signed_in.get("/help").text
        assert 'name="subject"' in text
        assert 'action="/app/support"' in text
        assert "Open the app" in text
        assert ">Log in<" not in text


class TestSending:
    def test_sends_and_shows_it(self, app, signed_in, user_id):
        response = signed_in.post(
            "/app/support", data={"subject": "Help", "body": "It broke"}, follow_redirects=True
        )
        assert "Message sent" in response.text
        assert "It broke" in response.text
        assert "Waiting for a reply" in response.text
        with app.app_context():
            message = db.session.scalar(select(SupportMessage))
            assert message.user_id == user_id
            assert message.status is SupportStatus.OPEN

    @pytest.mark.parametrize(
        ("subject", "body", "problem"),
        [
            ("", "Body", "The subject can&#39;t be empty"),
            ("Subject", "   ", "The message can&#39;t be empty"),
            ("x" * 201, "Body", "Use at most 200 characters"),
            ("Subject", "x" * 5001, "Use at most 5000 characters"),
        ],
    )
    def test_checks_what_it_sends(self, app, signed_in, subject, body, problem):
        response = signed_in.post("/app/support", data={"subject": subject, "body": body})
        assert response.status_code == 422
        assert problem in response.text
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(SupportMessage)) == 0

    def test_mistakes_dont_use_up_the_limit(self, signed_in):
        for _ in range(8):
            assert signed_in.post("/app/support", data={"subject": ""}).status_code == 422

    def test_sending_is_limited(self, app, signed_in):
        with app.app_context():
            limited = app.config.get("RATELIMIT_ENABLED", True)
        assert limited
        for number in range(5):
            response = signed_in.post(
                "/app/support", data={"subject": f"Message {number}", "body": "Text"}
            )
            assert response.status_code == 302
        refused = signed_in.post("/app/support", data={"subject": "Sixth", "body": "Text"})
        assert refused.status_code == 429
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(SupportMessage)) == 5

    def test_needs_a_login(self, client):
        assert client.get("/app/support").status_code == 302
        assert client.post("/app/support", data={"subject": "x", "body": "y"}).status_code == 302


class TestReading:
    def test_shows_the_reply(self, app, signed_in, user_id):
        with app.app_context():
            admin = auth.create_admin("chief", ADMIN_PASSWORD)
            message = support.send_message(user_id, subject="Help", body="Broke")
            support.reply(admin, message.id, "Try turning it off")
            message_id = message.id
        text = signed_in.get("/app/support").text
        assert "Try turning it off" in text
        assert f'id="message-{message_id}"' in text
        assert "Answered" in text

    def test_only_your_own(self, app, signed_in):
        with app.app_context():
            other = auth.register("other", PASSWORD).user.id
            support.send_message(other, subject="Private subject", body="Private body")
        text = signed_in.get("/app/support").text
        assert "Private subject" not in text
        assert "Your messages" not in text

    def test_what_you_wrote_is_escaped(self, app, signed_in, user_id):
        with app.app_context():
            support.send_message(user_id, subject="<script>a</script>", body="<script>b</script>")
        text = signed_in.get("/app/support").text
        assert "<script>a" not in text
        assert "<script>b" not in text

    def test_paging(self, app, signed_in, user_id):
        with app.app_context():
            for number in range(30):
                db.session.add(SupportMessage(user_id=user_id, subject=f"S{number}", body="B"))
            db.session.commit()
        assert "Page 1 of 2" in signed_in.get("/app/support").text


def test_a_null_byte_cant_reach_the_database(app, user_id):
    with app.app_context():
        with pytest.raises(ValidationFailed):
            support.send_message(user_id, subject="\x00", body="Body")
        message = support.send_message(user_id, subject="A\x00B", body="Body")
        assert message.subject == "AB"
