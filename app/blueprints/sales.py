"""Recording, finding, changing and downloading sales."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_login import current_user
from werkzeug.wrappers import Response as BaseResponse

from app.forms import show_errors, to_id
from app.forms.records import SaleFilterForm, SaleForm, options, product_options
from app.models import Partner
from app.security.rbac import login_required
from app.security.sessions import is_safe_redirect
from app.services import export, products, reporting, sales, split
from app.services.errors import LockedError, ValidationFailed
from app.services.sales import SaleFilters

bp = Blueprint("sales", __name__, url_prefix="/app/sales")

#: The most that can go in one undo. It's kept in the session cookie, which has limited room.
_MAX_BULK = sales.MAX_PER_PAGE
_INVALID = 422


@bp.get("")
@login_required
def list_sales() -> str:
    user_id = current_user.id
    people = split.list_people(user_id, include_archived=True)
    filter_form = _filter_form(user_id, people)
    filters = _filters(filter_form)
    page = sales.list_sales(user_id, filters, page=to_id(request.args.get("page")) or 1)
    return render_template(
        "pages/sales/list.html",
        filter_form=filter_form,
        filters=filters,
        page=page,
        people_names={person.id: person.name for person in people},
        show_receiver=len(people) > 1,
        has_products=bool(products.list_products(user_id)),
        filtered=bool(
            filters.search
            or filters.date_from
            or filters.date_to
            or filters.product_id
            or filters.received_by_partner_id
        ),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_sale() -> BaseResponse | str | tuple[str, int]:
    user_id = current_user.id
    form = SaleForm()
    product_rows = products.list_products(user_id)
    _fill_sale_choices(form, user_id, product_rows)

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
            flash("Sale saved.", "good")
            if "add_another" in request.form:
                return redirect(url_for("sales.new_sale"))
            return redirect(url_for("sales.list_sales"))
    elif request.method == "GET":
        form.sale_date.data = reporting.today_for(current_user).isoformat()
        form.quantity.data = "1"
        chosen = request.args.get("product")
        if chosen:
            form.product_id.data = chosen

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/sales/form.html", form=form, sale=None, has_products=bool(product_rows)
    ), status


@bp.route("/<int:sale_id>/edit", methods=["GET", "POST"])
@login_required
def edit_sale(sale_id: int) -> BaseResponse | str | tuple[str, int]:
    user_id = current_user.id
    sale = sales.get_sale(user_id, sale_id)
    locked = sale.settlement_id is not None
    form = SaleForm(obj=None if request.method == "POST" else sale)
    _fill_receiver_choices(form, user_id, current=sale.received_by_partner_id)

    if form.validate_on_submit():
        try:
            sales.update_sale(
                user_id,
                sale_id,
                sale_date=form.sale_date.data,
                # Locked fields are disabled in the page, so browsers don't send them.
                quantity=sale.quantity if locked else form.quantity.data,
                price_paid=sale.price_paid if locked else form.price_paid.data,
                customer_name=form.customer_name.data,
                notes=form.notes.data,
                received_by_partner_id=(
                    sale.received_by_partner_id
                    if locked
                    else to_id(form.received_by_partner_id.data)
                ),
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Sale updated.", "good")
            return redirect(url_for("sales.list_sales"))
    elif request.method == "GET":
        form.sale_date.data = sale.sale_date.isoformat()
        form.quantity.data = str(sale.quantity)
        form.price_paid.data = str(sale.price_paid)
        form.received_by_partner_id.data = str(sale.received_by_partner_id or "")

    status = _INVALID if form.errors else 200
    return render_template("pages/sales/form.html", form=form, sale=sale, locked=locked), status


@bp.post("/<int:sale_id>/delete")
@login_required
def delete_sale(sale_id: int) -> BaseResponse:
    try:
        sales.delete_sale(current_user.id, sale_id)
    except LockedError as error:
        flash(str(error), "error")
    else:
        session["undo_sales"] = [sale_id]
        flash("Sale deleted.", "good")
    return _back_to_list()


@bp.post("/bulk-delete")
@login_required
def bulk_delete() -> BaseResponse:
    ids = [i for i in (to_id(value) for value in request.form.getlist("ids")) if i][:_MAX_BULK]
    if not ids:
        flash("Tick the sales you want to delete first.", "error")
        return _back_to_list()
    result = sales.delete_sales(current_user.id, ids)
    if result.deleted_ids:
        count = len(result.deleted_ids)
        session["undo_sales"] = list(result.deleted_ids)
        flash(f"{count} sale{'' if count == 1 else 's'} deleted.", "good")
    if result.locked_ids:
        count = len(result.locked_ids)
        flash(
            f"{count} sale{' was' if count == 1 else 's were'} left alone, because "
            f"{'it is' if count == 1 else 'they are'} part of a settle-up.",
            "error",
        )
    return _back_to_list()


@bp.post("/restore")
@login_required
def restore() -> BaseResponse:
    ids = [i for i in (to_id(value) for value in request.form.getlist("ids")) if i][:_MAX_BULK]
    count = sales.restore_sales(current_user.id, ids)
    flash(f"{count} sale{'' if count == 1 else 's'} restored.", "good")
    return _back_to_list()


@bp.get("/export.csv")
@login_required
def export_csv() -> Response:
    """Everything matching the current filters, whichever page was showing."""
    user_id = current_user.id
    people = split.list_people(user_id, include_archived=True)
    rows = sales.iter_sales(user_id, _filters(_filter_form(user_id, people)))
    names = {person.id: person.name for person in people}
    today = reporting.today_for(current_user).isoformat()
    return Response(
        stream_with_context(export.sales_csv(rows, names)),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="selltrack-sales-{today}.csv"'},
    )


def _back_to_list() -> BaseResponse:
    target = request.form.get("next")
    if target and is_safe_redirect(target):
        return redirect(target)
    return redirect(url_for("sales.list_sales"))


def _filter_form(user_id: int, people: Iterable[Partner]) -> SaleFilterForm:
    form = SaleFilterForm(request.args)
    form.product.choices = options(
        [
            ("", "All products"),
            *(
                (row.product.id, row.product.name)
                for row in products.list_products(user_id, include_archived=True)
            ),
        ]
    )
    form.received_by.choices = options(
        [("", "Anyone"), *((person.id, person.name) for person in people)]
    )
    return form


def _filters(form: SaleFilterForm) -> SaleFilters:
    return SaleFilters(
        search=form.q.data or "",
        date_from=_date_or_none(form.date_from.data),
        date_to=_date_or_none(form.date_to.data),
        product_id=to_id(form.product.data),
        received_by_partner_id=to_id(form.received_by.data),
        sort=request.args.get("sort", "date"),  # type: ignore[arg-type]
        descending=request.args.get("dir", "desc") != "asc",
    )


def _date_or_none(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _fill_sale_choices(form: SaleForm, user_id: int, rows: list[products.ProductRow]) -> None:
    form.product_id.choices = product_options(rows)
    _fill_receiver_choices(form, user_id, current=None)
    if request.method == "GET":
        usual = next((p for p in split.list_people(user_id) if p.receives_money_default), None)
        if usual:
            form.received_by_partner_id.data = str(usual.id)


def _fill_receiver_choices(form: SaleForm, user_id: int, *, current: int | None) -> None:
    people = [
        person
        for person in split.list_people(user_id, include_archived=True)
        if person.archived_at is None or person.id == current
    ]
    form.received_by_partner_id.choices = options(
        [(person.id, person.name) for person in people] or [("", "Nobody yet")]
    )
