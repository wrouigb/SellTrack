from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, EqualTo, Length

from app.security.passwords import MAX_PASSWORD_LENGTH

_REQUIRED = DataRequired(message="This can't be empty.")
_NO_MATCH = "The two passwords don't match."


def _password_length() -> Length:
    # Rules like the minimum length live in the service, so there's one place to change them.
    return Length(max=MAX_PASSWORD_LENGTH, message=f"Use at most {MAX_PASSWORD_LENGTH} characters.")


class RegisterForm(FlaskForm):
    username = StringField("Username", validators=[_REQUIRED])
    password = PasswordField("Password", validators=[_REQUIRED, _password_length()])
    confirm = PasswordField(
        "Password again", validators=[_REQUIRED, EqualTo("password", message=_NO_MATCH)]
    )


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[_REQUIRED])
    password = PasswordField("Password", validators=[_REQUIRED])


class RecoverForm(FlaskForm):
    username = StringField("Username", validators=[_REQUIRED])
    code = StringField("Recovery code", validators=[_REQUIRED])
    new_password = PasswordField("New password", validators=[_REQUIRED, _password_length()])
    confirm = PasswordField(
        "New password again", validators=[_REQUIRED, EqualTo("new_password", message=_NO_MATCH)]
    )


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[_REQUIRED])
    new_password = PasswordField("New password", validators=[_REQUIRED, _password_length()])
    confirm = PasswordField(
        "New password again", validators=[_REQUIRED, EqualTo("new_password", message=_NO_MATCH)]
    )


class PasswordConfirmForm(FlaskForm):
    password = PasswordField("Current password", validators=[_REQUIRED])


class DeleteAccountForm(FlaskForm):
    password = PasswordField("Your password", validators=[_REQUIRED])
