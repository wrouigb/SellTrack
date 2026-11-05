"""The profit split: who owes whom, costs, payments, history and people."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response
from wtforms.fields.choices import SelectChoice

from app.forms import show_errors, to_id
from app.forms.records import (
    CATEGORY_CHOICES,
    CostForm,
    PaymentForm,
    PersonForm,
    category_label,
    options,
)
from app.security.rbac import login_required
from app.services import products, reporting, split
from app.services.errors import (
    CannotSettleError,
    ConflictError,
    LockedError,
    NotFoundError,
    ValidationFailed,
)
from app.services.split_engine import Issue

bp = Blueprint("split", __name__, url_prefix="/app/split")

_INVALID = 422


# -- Overview ---------------------------------------------------------------------------------


@bp.get("")
@login_required
def overview() -> str:
    view = split.current_period(current_user.id)
    return render_template("pages/split/overview.html", view=view, result=view.result, Issue=Issue)


@bp.route("/settle", methods=["GET", "POST"])
@login_required
def settle() -> Response | str:
    user_id = current_user.id
    if request.method == "POST":
        try:
            settlement = split.settle_up(user_id, actor_id=user_id)
        except CannotSettleError as error:
            flash(str(error), "error")
            return redirect(url_for("split.overview"))
        flash("Settled up. The tally is back to $0.00.", "good")
        return redirect(url_for("split.history_detail", settlement_id=settlement.id))
    view = split.current_period(user_id)
    return render_template("pages/split/settle.html", view=view, result=view.result)


# -- Upfront costs ----------------------------------------------------------------------------


@bp.route("/costs", methods=["GET", "POST"])
@login_required
def costs() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    form = CostForm()
    _fill_cost_choices(form, user_id)
    if form.validate_on_submit():
        try:
            split.add_cost(
                user_id,
                description=form.description.data,
                category=form.category.data,
                amount=form.amount.data,
                cost_date=form.cost_date.data,
                paid_by_partner_id=to_id(form.paid_by_partner_id.data) or 0,
                product_id=to_id(form.product_id.data),
                notes=form.notes.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Upfront cost added.", "good")
            return redirect(url_for("split.costs"))
    elif request.method == "GET":
        form.cost_date.data = reporting.today_for(current_user).isoformat()
        form.paid_by_partner_id.data = _usual_receiver(user_id)

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/split/costs.html",
        form=form,
        costs=split.list_costs(user_id),
        names=_names(user_id),
        has_people=bool(split.list_people(user_id)),
        category_label=category_label,
    ), status


@bp.route("/costs/<int:cost_id>/edit", methods=["GET", "POST"])
@login_required
def edit_cost(cost_id: int) -> Response | str | tuple[str, int]:
    user_id = current_user.id
    cost = split.get_cost(user_id, cost_id)
    locked = cost.settlement_id is not None
    form = CostForm()
    _fill_cost_choices(form, user_id, current_payer=cost.paid_by_partner_id)
    if form.validate_on_submit():
        try:
            split.update_cost(
                user_id,
                cost_id,
                description=form.description.data,
                category=form.category.data,
                amount=cost.amount if locked else form.amount.data,
                cost_date=form.cost_date.data,
                paid_by_partner_id=(
                    cost.paid_by_partner_id
                    if locked
                    else (to_id(form.paid_by_partner_id.data) or 0)
                ),
                product_id=to_id(form.product_id.data),
                notes=form.notes.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Upfront cost updated.", "good")
            return redirect(url_for("split.costs"))
    elif request.method == "GET":
        form.description.data = cost.description
        form.category.data = cost.category.value
        form.amount.data = str(cost.amount)
        form.cost_date.data = cost.cost_date.isoformat()
        form.paid_by_partner_id.data = str(cost.paid_by_partner_id)
        form.product_id.data = str(cost.product_id or "")
        form.notes.data = cost.notes

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/split/cost_edit.html", form=form, cost=cost, locked=locked
    ), status


@bp.post("/costs/<int:cost_id>/delete")
@login_required
def delete_cost(cost_id: int) -> Response:
    try:
        split.delete_cost(current_user.id, cost_id)
    except LockedError as error:
        flash(str(error), "error")
    else:
        flash("Upfront cost deleted.", "good")
    return redirect(url_for("split.costs"))


# -- Payments ---------------------------------------------------------------------------------


@bp.route("/payments", methods=["GET", "POST"])
@login_required
def payments() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    form = PaymentForm()
    _fill_payment_choices(form, user_id)
    if form.validate_on_submit():
        try:
            split.record_payment(
                user_id,
                from_partner_id=to_id(form.from_partner_id.data) or 0,
                to_partner_id=to_id(form.to_partner_id.data) or 0,
                amount=form.amount.data,
                paid_on=form.paid_on.data,
                note=form.note.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Payment recorded.", "good")
            return redirect(url_for("split.overview"))
    elif request.method == "GET":
        form.paid_on.data = reporting.today_for(current_user).isoformat()
        # Coming from a suggested transfer, the form starts filled in.
        form.from_partner_id.data = request.args.get("from")
        form.to_partner_id.data = request.args.get("to")
        form.amount.data = request.args.get("amount")

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/split/payments.html",
        form=form,
        payments=split.list_payments(user_id),
        names=_names(user_id),
        enough_people=len(split.list_people(user_id)) > 1,
    ), status


@bp.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
@login_required
def edit_payment(payment_id: int) -> Response | str | tuple[str, int]:
    user_id = current_user.id
    payment = split.get_payment(user_id, payment_id)
    locked = payment.settlement_id is not None
    form = PaymentForm()
    _fill_payment_choices(form, user_id, current=(payment.from_partner_id, payment.to_partner_id))
    if form.validate_on_submit():
        try:
            split.update_payment(
                user_id,
                payment_id,
                from_partner_id=(
                    payment.from_partner_id if locked else (to_id(form.from_partner_id.data) or 0)
                ),
                to_partner_id=(
                    payment.to_partner_id if locked else (to_id(form.to_partner_id.data) or 0)
                ),
                amount=payment.amount if locked else form.amount.data,
                paid_on=form.paid_on.data,
                note=form.note.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Payment updated.", "good")
            return redirect(url_for("split.payments"))
    elif request.method == "GET":
        form.from_partner_id.data = str(payment.from_partner_id)
        form.to_partner_id.data = str(payment.to_partner_id)
        form.amount.data = str(payment.amount)
        form.paid_on.data = payment.paid_on.isoformat()
        form.note.data = payment.note

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/split/payment_edit.html", form=form, payment=payment, locked=locked
    ), status


@bp.post("/payments/<int:payment_id>/delete")
@login_required
def delete_payment(payment_id: int) -> Response:
    try:
        split.delete_payment(current_user.id, payment_id)
    except LockedError as error:
        flash(str(error), "error")
    else:
        flash("Payment deleted.", "good")
    return redirect(url_for("split.payments"))


# -- History ----------------------------------------------------------------------------------


@bp.get("/history")
@login_required
def history() -> str:
    return render_template(
        "pages/split/history.html", settlements=split.list_settlements(current_user.id)
    )


@bp.get("/history/<int:settlement_id>")
@login_required
def history_detail(settlement_id: int) -> str:
    user_id = current_user.id
    settlement = split.get_settlement(user_id, settlement_id)
    latest = split.list_settlements(user_id, limit=1)
    return render_template(
        "pages/split/history_detail.html",
        settlement=settlement,
        is_latest=bool(latest) and latest[0].id == settlement.id,
    )


@bp.post("/history/undo")
@login_required
def undo() -> Response:
    user_id = current_user.id
    try:
        result = split.undo_last_settlement(user_id, actor_id=user_id)
    except (NotFoundError, ConflictError) as error:
        flash(str(error), "error")
        return redirect(url_for("split.history"))
    flash(f"Settle-up undone. {split.undo_message(result)}", "good")
    return redirect(url_for("split.overview"))


# -- People -----------------------------------------------------------------------------------


@bp.route("/people", methods=["GET", "POST"])
@login_required
def people() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    form = PersonForm()
    if form.validate_on_submit():
        try:
            person = split.add_person(
                user_id,
                name=form.name.data,
                share_pct=form.share_pct.data,
                usually_receives_money=bool(form.usually_receives_money.data),
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"{person.name} added.", "good")
            return redirect(url_for("split.people"))

    status = _INVALID if form.errors else 200
    return _people_page(form, row_errors={}), status


def _people_page(form: PersonForm, *, row_errors: dict[str, str]) -> str:
    everyone = split.list_people(current_user.id, include_archived=True)
    return render_template(
        "pages/split/people.html",
        form=form,
        active=[person for person in everyone if person.archived_at is None],
        archived=[person for person in everyone if person.archived_at is not None],
        row_errors=row_errors,
    )


@bp.post("/people/shares")
@login_required
def save_people() -> Response | tuple[str, int]:
    user_id = current_user.id
    changes = {
        person.id: (
            request.form.get(f"name_{person.id}", ""),
            request.form.get(f"share_{person.id}", ""),
        )
        for person in split.list_people(user_id)
    }
    try:
        split.update_people(user_id, changes)
    except ValidationFailed as error:
        return _people_page(PersonForm(formdata=None), row_errors=error.errors), _INVALID
    flash("People saved.", "good")
    return redirect(url_for("split.people"))


@bp.post("/people/even")
@login_required
def even_shares() -> Response:
    split.set_even_shares(current_user.id)
    flash("Shares set to an even split.", "good")
    return redirect(url_for("split.people"))


@bp.post("/people/<int:partner_id>/usual-receiver")
@login_required
def usual_receiver(partner_id: int) -> Response:
    try:
        person = split.set_usual_receiver(current_user.id, partner_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
    else:
        flash(f"{person.name} now usually receives the sale money.", "good")
    return redirect(url_for("split.people"))


@bp.post("/people/<int:partner_id>/archive")
@login_required
def archive_person(partner_id: int) -> Response:
    try:
        person = split.archive_person(current_user.id, partner_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
    else:
        flash(f"{person.name} archived. Their share is now 0%.", "good")
    return redirect(url_for("split.people"))


@bp.post("/people/<int:partner_id>/restore")
@login_required
def restore_person(partner_id: int) -> Response:
    try:
        person = split.restore_person(current_user.id, partner_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
    else:
        flash(f"{person.name} restored, with a 0% share.", "good")
    return redirect(url_for("split.people"))


@bp.post("/people/<int:partner_id>/delete")
@login_required
def delete_person(partner_id: int) -> Response:
    try:
        split.delete_person(current_user.id, partner_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
    else:
        flash("Person removed.", "good")
    return redirect(url_for("split.people"))


# -- Helpers ----------------------------------------------------------------------------------


def _names(user_id: int) -> dict[int, str]:
    return {p.id: p.name for p in split.list_people(user_id, include_archived=True)}


def _usual_receiver(user_id: int) -> str | None:
    usual = next((p for p in split.list_people(user_id) if p.receives_money_default), None)
    return str(usual.id) if usual else None


def _person_choices(user_id: int, *, also: tuple[int, ...] = ()) -> list[SelectChoice]:
    """Everyone active, plus anyone archived who's already on the record being edited."""
    return options(
        (person.id, person.name)
        for person in split.list_people(user_id, include_archived=True)
        if person.archived_at is None or person.id in also
    )


def _fill_cost_choices(form: CostForm, user_id: int, *, current_payer: int | None = None) -> None:
    form.category.choices = CATEGORY_CHOICES
    form.paid_by_partner_id.choices = _person_choices(
        user_id, also=(current_payer,) if current_payer else ()
    )
    form.product_id.choices = options(
        [
            ("", "No product"),
            *(
                (row.product.id, row.product.name)
                for row in products.list_products(user_id, include_archived=True)
            ),
        ]
    )


def _fill_payment_choices(
    form: PaymentForm, user_id: int, *, current: tuple[int, ...] = ()
) -> None:
    choices = _person_choices(user_id, also=current)
    form.from_partner_id.choices = choices
    form.to_partner_id.choices = choices
