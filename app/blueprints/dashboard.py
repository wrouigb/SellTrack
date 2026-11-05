"""The signed-in home page."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, render_template, request
from flask_login import current_user

from app.charts import build_chart
from app.forms.records import PeriodForm
from app.security.rbac import login_required
from app.services import reporting, split
from app.services.errors import ValidationFailed
from app.services.next_steps import next_steps
from app.services.reporting import Period

bp = Blueprint("dashboard", __name__, url_prefix="/app")

DEFAULT_PERIOD = "this_month"
_TOP_PRODUCTS = 5


@bp.get("")
@login_required
def home() -> str:
    form = PeriodForm(request.args)
    today = reporting.today_for(current_user)
    period, problem = _chosen_period(form, today)
    form.period.data = form.period.data or DEFAULT_PERIOD

    user_id = current_user.id
    summary = reporting.summary(user_id, period, today=today)
    bucket, points = reporting.series(user_id, period, today=today)
    current = split.current_period(user_id)
    return render_template(
        "pages/dashboard.html",
        form=form,
        problem=problem,
        summary=summary,
        compare=_compare_label(summary.previous_period, period, today),
        chart=build_chart(bucket, points),
        points=points,
        bucket=bucket,
        top_products=reporting.by_product(user_id, period, limit=_TOP_PRODUCTS),
        recent=reporting.recent_sales(user_id),
        split_result=current.result,
        has_people=bool(current.result.people),
        steps=next_steps(current_user, current.result),
    )


def _chosen_period(form: PeriodForm, today: date) -> tuple[Period, str | None]:
    """The period in the address. A bad one falls back rather than showing an error page."""
    key = form.period.data or DEFAULT_PERIOD
    start = _parse_date(form.date_from.data)
    end = _parse_date(form.date_to.data)
    if key == "custom" and start is None and end is None:
        # "Choose dates" was picked and the boxes are still empty. That's not a mistake yet.
        return reporting.resolve_period(DEFAULT_PERIOD, today), None
    try:
        return reporting.resolve_period(key, today, start=start, end=end), None
    except ValidationFailed as error:
        fallback = reporting.resolve_period(DEFAULT_PERIOD, today)
        return fallback, next(iter(error.errors.values()))


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _compare_label(previous: Period | None, period: Period, today: date) -> str:
    """How to name the period being compared with."""
    if previous is None or previous.start is None or previous.end is None:
        return ""
    in_progress = (
        period.start is not None and period.end is not None and (period.start <= today < period.end)
    )
    if period.key == "this_year":
        return "this time last year" if in_progress else str(previous.start.year)
    if period.key in ("this_month", "last_month"):
        month = f"{previous.start:%B}"
        return f"this time in {month}" if in_progress else month
    return "the period before"
