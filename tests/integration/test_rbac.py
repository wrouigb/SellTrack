"""Permission decorators, on a throwaway route."""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import UserStatus
from app.security.rbac import admin_required, login_required
from app.services import auth
from tests.conftest import login, make_settings
from tests.factories import PASSWORD

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "a-long-admin-passphrase"


@pytest.fixture
def rbac_app():
    application = create_app(make_settings())

    @application.get("/admin-only")
    @admin_required
    def admin_only():
        return "secret admin page"

    @application.get("/members-only")
    @login_required
    def members_only():
        return "members page"

    yield application
    create_app(make_settings())


@pytest.fixture
def accounts(rbac_app):
    with rbac_app.app_context():
        auth.register("regular", PASSWORD)
        auth.create_admin("chief", ADMIN_PASSWORD)


def test_guests_are_sent_to_login(rbac_app):
    response = rbac_app.test_client().get("/admin-only")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")


def test_normal_users_get_403(rbac_app, accounts):
    client = rbac_app.test_client()
    login(client, "regular", PASSWORD)
    response = client.get("/admin-only")
    assert response.status_code == 403
    assert "secret admin page" not in response.text
    assert client.get("/members-only").status_code == 200


def test_admins_are_let_in(rbac_app, accounts):
    client = rbac_app.test_client()
    login(client, "chief", ADMIN_PASSWORD)
    assert client.get("/admin-only").text == "secret admin page"


def test_suspended_admin_loses_access_immediately(rbac_app, accounts):
    client = rbac_app.test_client()
    login(client, "chief", ADMIN_PASSWORD)
    assert client.get("/admin-only").status_code == 200
    with rbac_app.app_context():
        other_admin = auth.create_admin("deputy", "a-second-admin-passphrase")
        chief = db.session.scalar(db.select(auth.User).where(auth.User.username == "chief"))
        auth.set_user_status(other_admin, chief.id, UserStatus.SUSPENDED)
    assert client.get("/admin-only").status_code == 302


def test_admins_cannot_suspend_themselves(rbac_app, accounts):
    with rbac_app.app_context():
        chief = db.session.scalar(db.select(auth.User).where(auth.User.username == "chief"))
        with pytest.raises(auth.ValidationFailed):
            auth.set_user_status(chief, chief.id, UserStatus.SUSPENDED)
