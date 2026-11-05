"""Forms for the admin console."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, SearchField, SelectField, StringField, TextAreaField
from wtforms.fields.choices import SelectChoice
from wtforms.widgets import DateInput, NumberInput

from app.services.audit import ACTION_LABELS


class UserFilterForm(FlaskForm):
    class Meta:
        csrf = False

    q = SearchField("Search usernames")
    status = SelectField(
        "Status",
        choices=[
            SelectChoice("", "Any status"),
            SelectChoice("active", "Active"),
            SelectChoice("suspended", "Suspended"),
        ],
        validate_choice=False,
    )
    role = SelectField(
        "Role",
        choices=[
            SelectChoice("", "Any role"),
            SelectChoice("user", "User"),
            SelectChoice("admin", "Admin"),
        ],
        validate_choice=False,
    )


class SupportFilterForm(FlaskForm):
    class Meta:
        csrf = False

    q = SearchField("Search subject or username")
    status = SelectField(
        "Show",
        choices=[
            SelectChoice("open", "Waiting for a reply"),
            SelectChoice("answered", "Answered"),
            SelectChoice("closed", "Closed"),
            SelectChoice("all", "All messages"),
        ],
        default="open",
        validate_choice=False,
    )


class SupportReplyForm(FlaskForm):
    reply = TextAreaField("Your reply")


class AnnouncementForm(FlaskForm):
    title = StringField("Title")
    body = TextAreaField("Message")
    url = StringField("Link (optional)")


class AuditFilterForm(FlaskForm):
    class Meta:
        csrf = False

    action = SelectField(
        "Action",
        choices=[
            SelectChoice("", "Any action"),
            *(
                SelectChoice(action, label)
                for action, label in sorted(ACTION_LABELS.items(), key=lambda item: item[1])
            ),
        ],
        validate_choice=False,
    )
    actor = SearchField("Done by (username)")
    date_from = StringField("From", widget=DateInput())
    date_to = StringField("To", widget=DateInput())


class NewFlagForm(FlaskForm):
    key = StringField("Name")
    description = StringField("What it does")


class EditFlagForm(FlaskForm):
    description = StringField("What it does")
    enabled = BooleanField("On")
    rollout_pct = StringField("Rollout (%)", widget=NumberInput(min=0, max=100, step=1))
    allow_users = TextAreaField("Always on for")
