"""Sign-up, login, logout, recovery and account security, through the real routes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import AuditLog, Notification, RecoveryCode, SignIn, User, UserStatus
from app.security import passwords
from app.services import auth, notifications
from app.services.notifications import Kind
from tests.conftest import login, recovery_codes_in, register
from tests.factories import PASSWORD, UserFactory

pytestmark = pytest.mark.integration

NEW_PASSWORD = "a-brand-new-passphrase"


def _user(app, username):
    with app.app_context():
        user = db.session.scalar(select(User).where(User.username == username))
        db.session.expunge(user)
        return user


def _new_user(app, username="alex", password=PASSWORD):
    with app.app_context():
        return auth.register(username, password)


def _audit_actions(app):
    with app.app_context():
        return list(db.session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


class TestRegister:
    def test_creates_account_logs_in_and_shows_codes_once(self, app, client):
        response = register(client, "alex", PASSWORD)
        assert response.status_code == 200
        codes = recovery_codes_in(response.text)
        assert len(codes) == auth.RECOVERY_CODE_COUNT
        assert len(set(codes)) == auth.RECOVERY_CODE_COUNT
        assert client.get("/app").status_code == 200

        # The page holding the codes must never be cached, and the codes aren't stored plain.
        assert response.headers["Cache-Control"] == "no-store"
        with app.app_context():
            stored = db.session.scalars(select(RecoveryCode.code_hash)).all()
        assert len(stored) == auth.RECOVERY_CODE_COUNT
        assert not set(stored) & set(codes)

    def test_password_is_hashed_with_argon2id(self, app, client):
        register(client, "alex", PASSWORD)
        assert _user(app, "alex").password_hash.startswith("$argon2id$")

    @pytest.mark.parametrize(
        ("username", "problem"),
        [
            ("ab", "3 to 30"),
            ("a" * 31, "3 to 30"),
            ("has space", "letters, numbers"),
            ("<script>", "letters, numbers"),
            ("ünïcode", "letters, numbers"),
        ],
    )
    def test_rejects_bad_usernames(self, client, username, problem):
        response = register(client, username, PASSWORD)
        assert response.status_code == 422
        assert problem in response.text

    @pytest.mark.parametrize(
        ("password", "problem"),
        [
            ("short", "at least 8"),
            ("password", "too easy to guess"),
            ("PassWord", "too easy to guess"),
            ("12345678", "too easy to guess"),
            ("aaaaaaaaaaaa", "too easy to guess"),
            ("x" * 129, "at most 128"),
            ("alex-is-my-name", "contain your username"),
        ],
    )
    def test_rejects_bad_passwords(self, client, password, problem):
        response = register(client, "alex", password)
        assert response.status_code == 422
        assert problem in response.text.replace("&#39;", "'")

    def test_rejects_mismatched_confirmation(self, client):
        response = client.post(
            "/register", data={"username": "alex", "password": PASSWORD, "confirm": "different-one"}
        )
        assert response.status_code == 422
        assert "match" in response.text

    def test_usernames_are_unique_ignoring_case(self, app, client):
        _new_user(app, "Alex")
        response = register(client, "aLEX", PASSWORD)
        assert response.status_code == 422
        assert "already taken" in response.text

    def test_keeps_what_the_user_typed_after_an_error(self, client):
        response = register(client, "alex", "short")
        assert 'value="alex"' in response.text
        assert "short" not in response.text.replace("short-", "")

    def test_nobody_can_register_as_admin(self, app, client):
        client.post(
            "/register",
            data={
                "username": "sneaky",
                "password": PASSWORD,
                "confirm": PASSWORD,
                "role": "admin",
                "is_admin": "true",
            },
        )
        assert not _user(app, "sneaky").is_admin

    def test_redirects_when_already_logged_in(self, client):
        register(client, "alex", PASSWORD)
        assert client.get("/register").status_code == 302


class TestLogin:
    def test_logs_in_and_stays_logged_in(self, app, client):
        _new_user(app)
        response = login(client, "alex", PASSWORD)
        assert response.status_code == 302
        assert response.headers["Location"] == "/app"
        assert "alex" in client.get("/app").text

    def test_username_is_case_insensitive(self, app, client):
        _new_user(app, "Alex")
        assert login(client, "ALEX", PASSWORD).status_code == 302

    def test_wrong_password_and_unknown_user_look_the_same(self, app, client):
        _new_user(app)
        wrong = login(client, "alex", "not-the-password")
        unknown = login(client, "nobody", "not-the-password")
        assert wrong.status_code == unknown.status_code == 401
        assert "Wrong username or password." in wrong.text
        assert "Wrong username or password." in unknown.text

    def test_unknown_user_still_costs_a_password_check(self, app, client, monkeypatch):
        """Audit issue 9: without this, response time reveals which usernames exist."""
        calls = []
        original = passwords.verify_password

        def spy(password_hash, password):
            calls.append(password)
            return original(password_hash, password)

        monkeypatch.setattr(passwords, "verify_password", spy)
        login(client, "nobody", "whatever-it-is")
        assert calls == ["whatever-it-is"]

    def test_impossibly_long_username_is_just_a_failed_login(self, client):
        assert login(client, "x" * 5000, PASSWORD).status_code == 401

    def test_session_is_replaced_at_login(self, app, client):
        """A session planted before login must not carry over into the logged-in one."""
        _new_user(app)
        with client.session_transaction() as session:
            session["planted"] = "by an attacker"
        login(client, "alex", PASSWORD)
        with client.session_transaction() as session:
            assert "planted" not in session

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("/app/settings/security", "/app/settings/security"),
            ("https://evil.example/", "/app"),
            ("//evil.example/", "/app"),
            ("/\\evil.example", "/app"),
            ("javascript:alert(1)", "/app"),
        ],
    )
    def test_next_only_goes_to_this_site(self, app, client, target, expected):
        _new_user(app)
        response = client.post(
            "/login",
            query_string={"next": target},
            data={"username": "alex", "password": PASSWORD},
        )
        assert response.headers["Location"] == expected

    def test_records_sign_in_with_ip_and_device(self, app, client):
        _new_user(app)
        login(client, "alex", PASSWORD, headers={"User-Agent": "TestBrowser/1.0"})
        with app.app_context():
            sign_in = db.session.scalar(select(SignIn))
            assert str(sign_in.ip) == "127.0.0.1"
            assert sign_in.user_agent == "TestBrowser/1.0"
            assert db.session.scalar(select(User.last_login_at)) is not None

    def test_notifies_only_about_a_new_device(self, app):
        _new_user(app)
        first, again, other = app.test_client(), app.test_client(), app.test_client()
        login(first, "alex", PASSWORD, headers={"User-Agent": "Phone"})
        login(again, "alex", PASSWORD, headers={"User-Agent": "Phone"})
        with app.app_context():
            assert db.session.scalar(select(func.count()).select_from(Notification)) == 0
        login(other, "alex", PASSWORD, headers={"User-Agent": "Stranger's laptop"})
        with app.app_context():
            notes = db.session.scalars(select(Notification)).all()
            assert [n.kind for n in notes] == [Kind.NEW_SIGN_IN.value]
            assert "Stranger's laptop" in notes[0].body

    def test_upgrades_a_weak_hash_after_login(self, app, client):
        _new_user(app)
        with app.app_context():
            user = db.session.scalar(select(User))
            app.config["ARGON2_TIME_COST"] = 1
            app.config["ARGON2_MEMORY_COST_KIB"] = 16
            try:
                user.password_hash = passwords.hash_password(PASSWORD)
                db.session.commit()
            finally:
                app.config["ARGON2_MEMORY_COST_KIB"] = 8
            weak = user.password_hash
        login(client, "alex", PASSWORD)
        assert _user(app, "alex").password_hash != weak

    def test_failed_and_successful_logins_are_audited_without_secrets(self, app, client):
        _new_user(app)
        login(client, "alex", "not-the-password")
        login(client, "nobody", "my-real-password-typed-wrongly")
        login(client, "alex", PASSWORD)
        assert _audit_actions(app)[-3:] == ["auth.login_failed", "auth.login_failed", "auth.login"]
        with app.app_context():
            for entry in db.session.scalars(select(AuditLog)):
                text = f"{entry.details}{entry.user_agent}"
                assert "not-the-password" not in text
                assert "nobody" not in text


class TestSuspended:
    def test_cannot_log_in(self, app, client):
        _new_user(app)
        with app.app_context():
            db.session.scalar(select(User)).status = UserStatus.SUSPENDED
            db.session.commit()
        response = login(client, "alex", PASSWORD)
        assert response.status_code == 401
        assert "suspended" in response.text

    def test_wrong_password_does_not_reveal_suspension(self, app, client):
        _new_user(app)
        with app.app_context():
            db.session.scalar(select(User)).status = UserStatus.SUSPENDED
            db.session.commit()
        assert "suspended" not in login(client, "alex", "not-the-password").text

    def test_existing_session_stops_working_straight_away(self, app, client):
        _new_user(app)
        login(client, "alex", PASSWORD)
        assert client.get("/app").status_code == 200
        with app.app_context():
            db.session.scalar(select(User)).status = UserStatus.SUSPENDED
            db.session.commit()
        assert client.get("/app").status_code == 302


class TestLogout:
    def test_ends_the_session(self, client):
        register(client, "alex", PASSWORD)
        assert client.post("/logout").status_code == 302
        assert client.get("/app").status_code == 302

    def test_get_is_not_allowed(self, client):
        register(client, "alex", PASSWORD)
        assert client.get("/logout").status_code == 405
        assert client.get("/app").status_code == 200


class TestLoginRequired:
    @pytest.mark.parametrize(
        "path", ["/app", "/app/settings/security", "/app/settings/security/password"]
    )
    def test_guests_go_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login?next=")


class TestChangePassword:
    def _change(self, client, current=PASSWORD, new=NEW_PASSWORD, confirm=None):
        return client.post(
            "/app/settings/security/password",
            data={
                "current_password": current,
                "new_password": new,
                "confirm": new if confirm is None else confirm,
            },
        )

    def test_changes_password_keeps_this_device_and_logs_out_others(self, app):
        _new_user(app)
        here, elsewhere = app.test_client(), app.test_client()
        login(here, "alex", PASSWORD)
        login(elsewhere, "alex", PASSWORD)

        assert self._change(here).status_code == 302

        assert here.get("/app").status_code == 200, "this device stays logged in"
        assert elsewhere.get("/app").status_code == 302, "other devices are logged out"
        fresh = app.test_client()
        assert login(fresh, "alex", PASSWORD).status_code == 401
        assert login(fresh, "alex", NEW_PASSWORD).status_code == 302

    def test_needs_the_current_password(self, app, client):
        register(client, "alex", PASSWORD)
        response = self._change(client, current="not-my-password")
        assert response.status_code == 422
        assert "current password" in response.text
        assert login(app.test_client(), "alex", PASSWORD).status_code == 302

    def test_applies_the_password_rules(self, client):
        register(client, "alex", PASSWORD)
        assert "too easy to guess" in self._change(client, new="password123").text

    def test_new_password_must_differ(self, client):
        register(client, "alex", PASSWORD)
        assert "different" in self._change(client, new=PASSWORD).text

    def test_confirmation_must_match(self, client):
        register(client, "alex", PASSWORD)
        assert "match" in self._change(client, confirm="something-else").text

    def test_notifies_and_audits(self, app, client):
        register(client, "alex", PASSWORD)
        self._change(client)
        assert "auth.password_changed" in _audit_actions(app)
        with app.app_context():
            kinds = db.session.scalars(select(Notification.kind)).all()
            assert Kind.PASSWORD_CHANGED.value in kinds


class TestLogoutEverywhere:
    def test_ends_every_session_including_this_one(self, app):
        _new_user(app)
        here, elsewhere = app.test_client(), app.test_client()
        login(here, "alex", PASSWORD)
        login(elsewhere, "alex", PASSWORD)

        assert here.post("/app/settings/security/logout-everywhere").status_code == 302

        assert here.get("/app").status_code == 302
        assert elsewhere.get("/app").status_code == 302
        assert login(app.test_client(), "alex", PASSWORD).status_code == 302

    def test_an_old_cookie_replayed_afterwards_is_rejected(self, app):
        _new_user(app)
        client = app.test_client()
        login(client, "alex", PASSWORD)
        stolen = client.get_cookie("selltrack_session").value

        client.post("/app/settings/security/logout-everywhere")

        attacker = app.test_client()
        attacker.set_cookie("selltrack_session", stolen)
        assert attacker.get("/app").status_code == 302


class TestRecovery:
    def _recover(self, client, username, code, password=NEW_PASSWORD, confirm=None):
        return client.post(
            "/recover",
            data={
                "username": username,
                "code": code,
                "new_password": password,
                "confirm": password if confirm is None else confirm,
            },
        )

    def test_code_resets_password_and_revokes_sessions(self, app):
        account = _new_user(app)
        existing = app.test_client()
        login(existing, "alex", PASSWORD)

        client = app.test_client()
        response = self._recover(client, "alex", account.recovery_codes[0])

        assert response.status_code == 302
        assert response.headers["Location"] == "/login"
        assert existing.get("/app").status_code == 302, "old sessions are revoked"
        assert login(app.test_client(), "alex", PASSWORD).status_code == 401
        assert login(app.test_client(), "alex", NEW_PASSWORD).status_code == 302
        with app.app_context():
            kinds = db.session.scalars(select(Notification.kind)).all()
            assert Kind.RECOVERY_CODE_USED.value in kinds

    def test_code_works_only_once(self, app, client):
        code = _new_user(app).recovery_codes[0]
        assert self._recover(client, "alex", code).status_code == 302
        response = self._recover(client, "alex", code, password="yet-another-passphrase")
        assert response.status_code == 401
        assert login(app.test_client(), "alex", NEW_PASSWORD).status_code == 302

    def test_wrong_code_and_unknown_user_look_the_same(self, app, client):
        code = _new_user(app).recovery_codes[0]
        wrong = self._recover(client, "alex", "AAAA-BBBB-CCCC-DDDD")
        unknown = self._recover(client, "nobody", code)
        assert wrong.status_code == unknown.status_code == 401
        assert "Wrong username or recovery code." in wrong.text
        assert "Wrong username or recovery code." in unknown.text

    def test_another_accounts_code_does_not_work(self, app, client):
        _new_user(app, "alex")
        sams_code = _new_user(app, "sam").recovery_codes[0]
        assert self._recover(client, "alex", sams_code).status_code == 401

    def test_forgives_how_a_code_was_typed(self, app, client):
        code = _new_user(app).recovery_codes[0]
        typed = code.lower().replace("-", " ")
        assert self._recover(client, "alex", typed).status_code == 302

    def test_weak_new_password_leaves_the_code_unused(self, app, client):
        code = _new_user(app).recovery_codes[0]
        response = self._recover(client, "alex", code, password="password")
        assert response.status_code == 422
        with app.app_context():
            assert auth.remaining_recovery_codes(db.session.scalar(select(User.id))) == 10
        assert self._recover(client, "alex", code).status_code == 302

    def test_warns_when_codes_run_low(self, app, client):
        codes = _new_user(app).recovery_codes
        for number, code in enumerate(codes[:8]):
            password = f"another-good-passphrase-{number}"
            assert self._recover(client, "alex", code, password=password).status_code == 302
        with app.app_context():
            user_id = db.session.scalar(select(User.id))
            assert auth.remaining_recovery_codes(user_id) == 2
            titles = [n.title for n in notifications.list_for(user_id)]
            assert "2 recovery codes left" in titles

    def test_suspended_account_cannot_recover(self, app, client):
        code = _new_user(app).recovery_codes[0]
        with app.app_context():
            db.session.scalar(select(User)).status = UserStatus.SUSPENDED
            db.session.commit()
        response = self._recover(client, "alex", code)
        assert response.status_code == 403
        with app.app_context():
            assert auth.remaining_recovery_codes(db.session.scalar(select(User.id))) == 10


class TestRegenerateRecoveryCodes:
    def test_replaces_codes_and_old_ones_stop_working(self, app, client):
        response = register(client, "alex", PASSWORD)
        old_code = recovery_codes_in(response.text)[0]

        response = client.post("/app/settings/security/recovery-codes", data={"password": PASSWORD})
        new_codes = recovery_codes_in(response.text)
        assert len(new_codes) == 10
        assert old_code not in new_codes
        assert response.headers["Cache-Control"] == "no-store"

        with app.app_context():
            with pytest.raises(Exception, match="Wrong username or recovery code"):
                auth.recover_account("alex", old_code, NEW_PASSWORD)
            auth.recover_account("alex", new_codes[0], NEW_PASSWORD)

    def test_needs_the_password(self, app, client):
        code = recovery_codes_in(register(client, "alex", PASSWORD).text)[0]
        response = client.post(
            "/app/settings/security/recovery-codes", data={"password": "not-my-password"}
        )
        assert response.status_code == 302
        with app.app_context():
            auth.recover_account("alex", code, NEW_PASSWORD)  # still valid


class TestSecurityPage:
    def test_lists_recent_sign_ins_and_remaining_codes(self, app, client):
        _new_user(app)
        login(client, "alex", PASSWORD, headers={"User-Agent": "TestBrowser/1.0"})
        page = client.get("/app/settings/security")
        assert page.status_code == 200
        assert "TestBrowser/1.0" in page.text
        assert "10 unused recovery codes" in page.text

    def test_only_shows_your_own_sign_ins(self, app):
        _new_user(app, "alex")
        _new_user(app, "sam")
        alex, sam = app.test_client(), app.test_client()
        login(alex, "alex", PASSWORD, headers={"User-Agent": "AlexPhone"})
        login(sam, "sam", PASSWORD, headers={"User-Agent": "SamPhone"})
        assert "SamPhone" not in alex.get("/app/settings/security").text


class TestAdminReset:
    def test_temporary_password_must_be_changed_before_anything_else(self, app, ctx):
        account = auth.register("alex", PASSWORD)
        admin = auth.create_admin("boss", "the-chief-passphrase")
        temporary = auth.admin_reset_password(admin, account.user.id)

        client = app.test_client()
        assert login(client, "alex", PASSWORD).status_code == 401, (
            "the old password stopped working"
        )
        assert login(client, "alex", temporary).status_code == 302

        for path in ("/app", "/"):
            response = client.get(path)
            assert response.status_code == 302
            assert response.headers["Location"] == "/app/settings/security/password"

        change_page = client.get("/app/settings/security/password")
        assert change_page.status_code == 200

        response = client.post(
            "/app/settings/security/password",
            data={
                "current_password": temporary,
                "new_password": NEW_PASSWORD,
                "confirm": NEW_PASSWORD,
            },
        )
        assert response.status_code == 302
        assert client.get("/app").status_code == 200

    def test_reset_is_audited(self, ctx):
        account = auth.register("alex", PASSWORD)
        admin = auth.create_admin("boss", "the-chief-passphrase")
        auth.admin_reset_password(admin, account.user.id)
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "admin.password_reset"))
        assert entry.actor_id == admin.id
        assert entry.target_id == account.user.id


class TestRateLimits:
    def test_login_is_limited_per_username_on_failures_only(self, app, client):
        _new_user(app)
        for _ in range(5):
            assert login(client, "alex", "not-the-password").status_code == 401
        blocked = login(client, "alex", "not-the-password")
        assert blocked.status_code == 429
        assert "Too many attempts" in blocked.text
        assert "Retry-After" in blocked.headers
        # The right password doesn't help once locked out.
        assert login(client, "alex", PASSWORD).status_code == 429
        # Someone else isn't affected.
        assert login(client, "someone-else", "not-the-password").status_code == 401

    def test_successful_logins_do_not_count_against_the_username(self, app):
        _new_user(app)
        for _ in range(8):
            client = app.test_client()
            assert login(client, "alex", PASSWORD).status_code == 302

    def test_username_limit_is_case_insensitive(self, app, client):
        _new_user(app)
        for name in ("alex", "ALEX", "Alex", "aLEX", "alEX"):
            login(client, name, "not-the-password")
        assert login(client, "ALex", "not-the-password").status_code == 429

    def test_login_is_limited_per_ip(self, client):
        for number in range(20):
            assert login(client, f"user{number}", "not-the-password").status_code == 401
        assert login(client, "another", "not-the-password").status_code == 429

    def test_register_is_limited_per_ip(self, client):
        for number in range(10):
            client.post("/logout")
            assert register(client, f"newuser{number}", PASSWORD).status_code == 200
        client.post("/logout")
        assert register(client, "onemore", PASSWORD).status_code == 429

    def test_recovery_is_limited_per_username(self, app, client):
        _new_user(app)
        data = {
            "username": "alex",
            "code": "AAAA-BBBB-CCCC-DDDD",
            "new_password": NEW_PASSWORD,
            "confirm": NEW_PASSWORD,
        }
        for _ in range(5):
            assert client.post("/recover", data=data).status_code == 401
        assert client.post("/recover", data=data).status_code == 429

    def test_password_checks_are_limited_per_account(self, client):
        register(client, "alex", PASSWORD)
        data = {"password": "not-my-password"}
        for _ in range(10):
            assert (
                client.post("/app/settings/security/recovery-codes", data=data).status_code == 302
            )
        assert client.post("/app/settings/security/recovery-codes", data=data).status_code == 429

    def test_viewing_a_form_does_not_count(self, client):
        for _ in range(30):
            assert client.get("/login").status_code == 200


class TestDeleteAccount:
    def test_needs_the_password(self, ctx):
        account = auth.register("alex", PASSWORD)
        with pytest.raises(Exception, match="current password"):
            auth.delete_account(account.user, "not-my-password")
        assert db.session.scalar(select(func.count()).select_from(User)) == 1

    def test_removes_the_account_but_keeps_the_audit_trail(self, ctx):
        account = auth.register("alex", PASSWORD)
        user_id = account.user.id
        auth.delete_account(account.user, PASSWORD)
        assert db.session.get(User, user_id) is None
        assert db.session.scalar(select(func.count()).select_from(RecoveryCode)) == 0
        entry = db.session.scalar(select(AuditLog).where(AuditLog.action == "auth.registered"))
        assert entry.actor_id is None, "kept, but no longer linked to the deleted account"
        assert entry.target_id == user_id


class TestAdminAccountCli:
    def test_creates_an_admin(self, app, clean_state):
        runner = app.test_cli_runner()
        result = runner.invoke(
            args=[
                "users",
                "create-admin",
                "--username",
                "boss",
                "--password",
                "the-chief-passphrase",
            ]
        )
        assert result.exit_code == 0, result.output
        assert _user(app, "boss").is_admin

    def test_refuses_a_weak_password(self, app, clean_state):
        runner = app.test_cli_runner()
        result = runner.invoke(
            args=["users", "create-admin", "--username", "boss", "--password", "password"]
        )
        assert result.exit_code == 1
        assert "too easy to guess" in result.output

    def test_refuses_a_taken_username(self, app, clean_state):
        _new_user(app, "boss")
        runner = app.test_cli_runner()
        result = runner.invoke(
            args=[
                "users",
                "create-admin",
                "--username",
                "boss",
                "--password",
                "the-chief-passphrase",
            ]
        )
        assert result.exit_code == 1
        assert not _user(app, "boss").is_admin


class TestUnused:
    def test_session_lifetime_is_seven_days(self, app):
        assert app.permanent_session_lifetime == timedelta(days=7)


def test_user_factory_password_works(app, ctx):
    user = UserFactory()
    db.session.commit()
    assert login(app.test_client(), user.username, PASSWORD).status_code == 302
