"""Public pages, and redirects from the addresses the original app used."""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from app.forms.records import SupportForm

bp = Blueprint("public", __name__)

_MOVED_PERMANENTLY = 301


@bp.get("/")
def landing() -> Response | str:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))
    return render_template("pages/landing.html")


@bp.get("/help")
def help_page() -> str:
    # Only for someone who can use it: making a form puts a CSRF token in the session cookie.
    form = SupportForm() if current_user.is_authenticated else None
    return render_template("pages/help.html", support_form=form)


@bp.get("/privacy")
def privacy() -> str:
    return render_template("pages/privacy.html")


@bp.get("/terms")
def terms() -> str:
    return render_template("pages/terms.html")


# Bookmarks from the original app keep working.
@bp.get("/sales")
def old_sales() -> Response:
    return redirect(url_for("sales.list_sales"), _MOVED_PERMANENTLY)


@bp.get("/edit/<int:sale_id>")
def old_edit_sale(sale_id: int) -> Response:
    return redirect(url_for("sales.edit_sale", sale_id=sale_id), _MOVED_PERMANENTLY)


@bp.get("/products")
def old_products() -> Response:
    return redirect(url_for("products.list_products"), _MOVED_PERMANENTLY)


@bp.get("/products/<int:product_id>")
def old_product(product_id: int) -> Response:
    return redirect(url_for("products.detail", product_id=product_id), _MOVED_PERMANENTLY)


@bp.get("/split")
def old_split() -> Response:
    return redirect(url_for("split.overview"), _MOVED_PERMANENTLY)
