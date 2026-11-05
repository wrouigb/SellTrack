"""Products, their costs, and archiving."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from app.forms import show_errors
from app.forms.records import ProductForm, ProductSearchForm
from app.security.rbac import login_required
from app.services import products
from app.services.errors import ValidationFailed

bp = Blueprint("products", __name__, url_prefix="/app/products")

_INVALID = 422
_SORTS = {"name", "cost", "sell_price", "margin", "sales"}


@bp.route("", methods=["GET", "POST"])
@login_required
def list_products() -> Response | str | tuple[str, int]:
    user_id = current_user.id
    form = ProductForm()
    if form.validate_on_submit():
        try:
            product = products.create_product(
                user_id,
                name=form.name.data,
                cost=form.cost.data,
                sell_price=form.sell_price.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash(f"{product.name} added.", "good")
            return redirect(url_for("products.list_products"))

    search = ProductSearchForm(request.args)
    sort = request.args.get("sort", "name")
    descending = request.args.get("dir", "asc") == "desc"
    rows = products.list_products(
        user_id,
        search=search.q.data or "",
        include_archived=bool(search.archived.data),
        sort=sort if sort in _SORTS else "name",  # type: ignore[arg-type]
        descending=descending,
    )
    status = _INVALID if form.errors else 200
    return render_template(
        "pages/products/list.html",
        form=form,
        search=search,
        rows=rows,
        sort=sort,
        descending=descending,
        searching=bool(search.q.data),
    ), status


@bp.route("/<int:product_id>", methods=["GET", "POST"])
@login_required
def detail(product_id: int) -> Response | str | tuple[str, int]:
    user_id = current_user.id
    product = products.get_product(user_id, product_id)
    form = ProductForm()
    if form.validate_on_submit():
        try:
            products.update_product(
                user_id,
                product_id,
                name=form.name.data,
                cost=form.cost.data,
                sell_price=form.sell_price.data,
            )
        except ValidationFailed as error:
            show_errors(form, error)
        else:
            flash("Product updated. New sales will use the new cost.", "good")
            return redirect(url_for("products.list_products"))
    elif request.method == "GET":
        form.name.data = product.name
        form.cost.data = str(product.cost)
        form.sell_price.data = "" if product.sell_price is None else str(product.sell_price)

    status = _INVALID if form.errors else 200
    return render_template(
        "pages/products/detail.html",
        form=form,
        product=product,
        history=products.cost_history(user_id, product_id),
        margin=products.margin_percent(product.cost, product.sell_price),
    ), status


@bp.post("/<int:product_id>/archive")
@login_required
def archive(product_id: int) -> Response:
    product = products.archive_product(current_user.id, product_id)
    flash(f"{product.name} archived. Its past sales are kept.", "good")
    return redirect(url_for("products.list_products"))


@bp.post("/<int:product_id>/restore")
@login_required
def restore(product_id: int) -> Response:
    try:
        product = products.restore_product(current_user.id, product_id)
    except ValidationFailed as error:
        flash(next(iter(error.errors.values())), "error")
        return redirect(url_for("products.detail", product_id=product_id))
    flash(f"{product.name} restored.", "good")
    return redirect(url_for("products.list_products"))


@bp.post("/<int:product_id>/delete")
@login_required
def delete(product_id: int) -> Response:
    products.delete_product(current_user.id, product_id)
    flash("Product deleted. Your past sales were kept.", "good")
    return redirect(url_for("products.list_products"))
