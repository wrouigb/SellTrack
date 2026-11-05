"""Products and their cost history."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Product, ProductCostChange, Sale
from app.services import entitlements
from app.services.errors import ConflictError, NotFoundError, ValidationFailed
from app.services.validation import Errors, checked, clean_money, clean_text, violated_constraint

NAME_MAX = 100
_NAME_TAKEN = "You already have a product with that name."
_NAME_INDEX = "uq_products_user_lower_name"
_ONE_DECIMAL = Decimal("0.1")

ProductSort = Literal["name", "cost", "sell_price", "margin", "sales"]


@dataclass(frozen=True, slots=True)
class ProductRow:
    """A product as shown in the list, with its figures."""

    product: Product
    sale_count: int
    margin_pct: Decimal | None


def margin_percent(cost: Decimal, sell_price: Decimal | None) -> Decimal | None:
    """Profit as a percentage of the selling price, or ``None`` if there's no price to use."""
    if not sell_price:
        return None
    return ((sell_price - cost) * 100 / sell_price).quantize(_ONE_DECIMAL, ROUND_HALF_UP)


def get_product(user_id: int, product_id: int, *, for_update: bool = False) -> Product:
    query = select(Product).where(Product.id == product_id, Product.user_id == user_id)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    product = db.session.scalar(query)
    if product is None:
        raise NotFoundError("That product doesn't exist.")
    return product


def create_product(
    user_id: int,
    *,
    name: str,
    cost: Decimal | int | str,
    sell_price: Decimal | int | str | None = None,
) -> Product:
    """Add a product and write the first line of its cost history."""
    errors = Errors()
    clean_name = clean_text(
        errors, "name", name, max_length=NAME_MAX, required=True, label="The name"
    )
    clean_cost = clean_money(errors, "cost", cost)
    clean_price = clean_money(errors, "sell_price", sell_price, required=False)
    errors.raise_if_any()
    entitlements.check(user_id, entitlements.Limit.PRODUCTS)

    product = Product(
        user_id=user_id, name=checked(clean_name), cost=checked(clean_cost), sell_price=clean_price
    )
    db.session.add(product)
    try:
        db.session.flush()
    except IntegrityError as error:
        db.session.rollback()
        raise _name_conflict(error) from None
    db.session.add(ProductCostChange(product_id=product.id, old_cost=None, new_cost=product.cost))
    db.session.commit()
    return product


def update_product(
    user_id: int,
    product_id: int,
    *,
    name: str,
    cost: Decimal | int | str,
    sell_price: Decimal | int | str | None = None,
) -> Product:
    """Change a product.

    A new cost is written to the history and only affects new sales: old sales keep the cost
    they were made at. A new name flows through to past sales.
    """
    errors = Errors()
    clean_name = clean_text(
        errors, "name", name, max_length=NAME_MAX, required=True, label="The name"
    )
    clean_cost = clean_money(errors, "cost", cost)
    clean_price = clean_money(errors, "sell_price", sell_price, required=False)
    errors.raise_if_any()

    new_name, new_cost = checked(clean_name), checked(clean_cost)

    product = get_product(user_id, product_id, for_update=True)
    if new_cost != product.cost:
        db.session.add(
            ProductCostChange(product_id=product.id, old_cost=product.cost, new_cost=new_cost)
        )
    if new_name != product.name:
        db.session.execute(
            update(Sale)
            .where(Sale.product_id == product.id, Sale.user_id == user_id)
            .values(product_name=new_name)
        )
    product.name = new_name
    product.cost = new_cost
    product.sell_price = clean_price
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise _name_conflict(error) from None
    return product


def archive_product(user_id: int, product_id: int) -> Product:
    """Hide a product from new sales. Its history and past sales are kept."""
    product = get_product(user_id, product_id)
    if product.archived_at is None:
        product.archived_at = datetime.now(UTC)
    db.session.commit()
    return product


def restore_product(user_id: int, product_id: int) -> Product:
    """Bring back an archived product, unless another product has taken its name since."""
    product = get_product(user_id, product_id)
    product.archived_at = None
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        raise _name_conflict(error) from None
    return product


def delete_product(user_id: int, product_id: int) -> None:
    """Remove a product and its cost history. Past sales stay, without the link to it."""
    product = get_product(user_id, product_id)
    db.session.delete(product)  # the foreign keys unlink sales and remove cost history
    db.session.commit()


def cost_history(user_id: int, product_id: int) -> Sequence[ProductCostChange]:
    """Every cost this product has had, newest first."""
    product = get_product(user_id, product_id)
    return db.session.scalars(
        select(ProductCostChange)
        .where(ProductCostChange.product_id == product.id)
        .order_by(ProductCostChange.changed_at.desc(), ProductCostChange.id.desc())
    ).all()


def list_products(
    user_id: int,
    *,
    search: str = "",
    include_archived: bool = False,
    sort: ProductSort = "name",
    descending: bool = False,
) -> list[ProductRow]:
    """All products with their sale counts, using one query however many products there are."""
    counts = (
        select(Sale.product_id, func.count().label("sale_count"))
        .where(Sale.user_id == user_id, Sale.deleted_at.is_(None))
        .group_by(Sale.product_id)
        .subquery()
    )
    sale_count = func.coalesce(counts.c.sale_count, 0)
    margin = case(
        (Product.sell_price > 0, (Product.sell_price - Product.cost) / Product.sell_price),
        else_=None,
    )
    columns = {
        "name": func.lower(Product.name),
        "cost": Product.cost,
        "sell_price": Product.sell_price,
        "margin": margin,
        "sales": sale_count,
    }
    if sort not in columns:
        raise ValidationFailed(sort="That isn't a column you can sort by.")
    order = columns[sort].desc().nulls_last() if descending else columns[sort].asc().nulls_last()

    query = (
        select(Product, sale_count)
        .outerjoin(counts, counts.c.product_id == Product.id)
        .where(Product.user_id == user_id)
        .order_by(order, Product.id)
    )
    if not include_archived:
        query = query.where(Product.archived_at.is_(None))
    if term := search.strip():
        query = query.where(Product.name.ilike(like_pattern(term), escape="\\"))
    return [
        ProductRow(
            product=product,
            sale_count=count,
            margin_pct=margin_percent(product.cost, product.sell_price),
        )
        for product, count in db.session.execute(query)
    ]


def like_pattern(term: str) -> str:
    """A ``%term%`` pattern where ``%`` and ``_`` in the search text are matched literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _name_conflict(error: IntegrityError) -> ConflictError:
    if violated_constraint(error) == _NAME_INDEX:
        return ConflictError(name=_NAME_TAKEN)
    raise error
