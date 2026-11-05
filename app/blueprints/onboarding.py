"""The first run: time zone, people, first product, first sale. Every step can be skipped."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from app.forms import show_errors, to_id
from app.forms.records import (
    PersonForm,
    ProductForm,
    SaleForm,
    TimezoneForm,
    options,
    product_options,
    timezone_options,
)
from app.models import DEFAULT_TIMEZONE
from app.security.rbac import login_required
from app.services import auth, products, reporting, sales, split
from app.services.errors import ValidationFailed

bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

_INVALID = 422


@bp.route("", methods=["GET", "POST"])
@login_required
def start() -> Response | str | tuple[str, int]:
    form = TimezoneForm()
    form.timezone.choices = timezone_options()
    if form.validate_on_submit():
        try:
            auth.update_preferences(current_user, timezone=form.timezone.data)
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            return redirect(url_for("onboarding.people"))
    elif request.method == "GET":
        form.timezone.data = current_user.timezone
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/onboarding/timezone.html",
        form=form,
        step=1,
        chosen=current_user.timezone != DEFAULT_TIMEZONE,
    ), status


@bp.route("/people", methods=["GET", "POST"])
@login_required
def people() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    form = PersonForm()
    if form.validate_on_submit():
        try:
            person = split.add_person(user_id, name=form.name.data, share_pct=form.share_pct.data)
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"{person.name} added.", "good")
            return redirect(url_for("onboarding.people"))
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/onboarding/people.html", form=form, step=2, people=split.list_people(user_id)
    ), status


@bp.post("/people/even")
@login_required
def even() -> Response:
    split.set_even_shares(current_user.id)
    flash("Shares set to an even split.", "good")
    return redirect(url_for("onboarding.people"))


@bp.route("/product", methods=["GET", "POST"])
@login_required
def product() -> Response | str | tuple[str, int]:
    form = ProductForm()
    if form.validate_on_submit():
        try:
            created = products.create_product(
                current_user.id,
                name=form.name.data,
                cost=form.cost.data,
                sell_price=form.sell_price.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"{created.name} added.", "good")
            return redirect(url_for("onboarding.sale"))
    status = _INVALID if form.errors else 200
    return render_template("pages/onboarding/product.html", form=form, step=3), status


@bp.route("/sale", methods=["GET", "POST"])
@login_required
def sale() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    rows = products.list_products(user_id)
    form = SaleForm()
    form.product_id.choices = product_options(rows)
    active = split.list_people(user_id)
    form.received_by_partner_id.choices = options(
        [(p.id, p.name) for p in active] or [("", "Nobody yet")]
    )
    if form.validate_on_submit():
        try:
            sales.create_sale(
                user_id,
                product_id=to_id(form.product_id.data) or 0,
                sale_date=form.sale_date.data,
                quantity=form.quantity.data,
                price_paid=form.price_paid.data,
                customer_name=form.customer_name.data,
                notes=form.notes.data,
                received_by_partner_id=to_id(form.received_by_partner_id.data),
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            auth.complete_onboarding(current_user)
            flash("You're all set. Your first sale is saved.", "good")
            return redirect(url_for("dashboard.home"))
    elif request.method == "GET":
        form.sale_date.data = reporting.today_for(current_user).isoformat()
        form.quantity.data = "1"
        if rows:
            form.product_id.data = str(rows[0].product.id)
        usual = next((p for p in active if p.receives_money_default), None)
        if usual:
            form.received_by_partner_id.data = str(usual.id)
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/onboarding/sale.html", form=form, step=4, has_products=bool(rows)
    ), status


@bp.post("/finish")
@login_required
def finish() -> Response:
    """Skip the rest. The dashboard points back to anything that's still worth doing."""
    auth.complete_onboarding(current_user)
    return redirect(url_for("dashboard.home"))
