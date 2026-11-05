"""Sales: recording, editing, deleting with undo, and the filtered list.

Each sale keeps a copy of the product's cost at that moment (``cost_each``), so changing a
product's cost never alters old profit figures.

Once a sale is part of a settle-up its money fields are locked until that settle-up is undone,
because the settlement's figures were worked out from them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, func, or_, select

from app.extensions import db
from app.models import Partner, Sale
from app.money import MAX_AMOUNT
from app.services.errors import LockedError, NotFoundError
from app.services.products import get_product, like_pattern
from app.services.validation import (
    Errors,
    checked,
    clean_date,
    clean_money,
    clean_quantity,
    clean_text,
)

CUSTOMER_MAX = 100
NOTES_MAX = 1000
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
_LOCKED = "This sale is part of a settle-up. Undo the settle-up to change its money."

SaleSort = Literal["date", "product", "customer", "quantity", "price", "profit"]


@dataclass(frozen=True, slots=True)
class SaleFilters:
    search: str = ""
    date_from: date | None = None
    date_to: date | None = None
    product_id: int | None = None
    customer: str = ""
    received_by_partner_id: int | None = None
    sort: SaleSort = "date"
    descending: bool = True


@dataclass(frozen=True, slots=True)
class SaleTotals:
    """Totals for the filtered rows, across every page."""

    count: int
    items_sold: int
    money_in: Decimal
    total_cost: Decimal
    profit: Decimal


@dataclass(frozen=True, slots=True)
class SalePage:
    items: Sequence[Sale]
    page: int
    per_page: int
    totals: SaleTotals

    @property
    def pages(self) -> int:
        return max(1, -(-self.totals.count // self.per_page))

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def has_previous(self) -> bool:
        return self.page > 1


@dataclass(frozen=True, slots=True)
class BulkDeleteResult:
    deleted_ids: tuple[int, ...]
    #: Sales left alone because they're part of a settle-up.
    locked_ids: tuple[int, ...]


def create_sale(
    user_id: int,
    *,
    product_id: int,
    sale_date: date | str | None,
    quantity: int | str | None,
    price_paid: Decimal | int | str | None,
    customer_name: str | None = None,
    notes: str | None = None,
    received_by_partner_id: int | None = None,
) -> Sale:
    """Record a sale, copying the product's current cost onto it."""
    errors = Errors()
    clean_day = clean_date(errors, "sale_date", sale_date)
    clean_qty = clean_quantity(errors, "quantity", quantity)
    clean_price = clean_money(errors, "price_paid", price_paid)
    clean_customer = clean_text(errors, "customer_name", customer_name, max_length=CUSTOMER_MAX)
    clean_notes = clean_text(errors, "notes", notes, max_length=NOTES_MAX)

    try:
        product = get_product(user_id, product_id)
    except NotFoundError:
        errors.add("product_id", "Choose one of your products.")
        product = None
    if product is not None and product.archived_at is not None:
        errors.add("product_id", "That product is archived. Restore it to sell it again.")

    receiver = _resolve_receiver(errors, user_id, received_by_partner_id)
    if product is not None and clean_qty is not None and clean_price is not None:
        _check_fits(errors, clean_qty, clean_price, product.cost)
    errors.raise_if_any()

    sold = checked(product)
    sale = Sale(
        user_id=user_id,
        product_id=sold.id,
        product_name=sold.name,
        cost_each=sold.cost,
        sale_date=checked(clean_day),
        quantity=checked(clean_qty),
        price_paid=checked(clean_price),
        customer_name=clean_customer,
        notes=clean_notes,
        received_by_partner_id=receiver,
    )
    db.session.add(sale)
    db.session.commit()
    return sale


def get_sale(user_id: int, sale_id: int, *, for_update: bool = False) -> Sale:
    query = select(Sale).where(
        Sale.id == sale_id, Sale.user_id == user_id, Sale.deleted_at.is_(None)
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    sale = db.session.scalar(query)
    if sale is None:
        raise NotFoundError("That sale doesn't exist.")
    return sale


def update_sale(
    user_id: int,
    sale_id: int,
    *,
    sale_date: date | str | None,
    quantity: int | str | None,
    price_paid: Decimal | int | str | None,
    customer_name: str | None = None,
    notes: str | None = None,
    received_by_partner_id: int | None = None,
) -> Sale:
    """Edit a sale. The product and its cost at the time stay as they were."""
    # Locked so this can't interleave with a settle-up reading the same row.
    sale = get_sale(user_id, sale_id, for_update=True)

    errors = Errors()
    clean_day = clean_date(errors, "sale_date", sale_date)
    clean_qty = clean_quantity(errors, "quantity", quantity)
    clean_price = clean_money(errors, "price_paid", price_paid)
    clean_customer = clean_text(errors, "customer_name", customer_name, max_length=CUSTOMER_MAX)
    clean_notes = clean_text(errors, "notes", notes, max_length=NOTES_MAX)
    receiver = (
        sale.received_by_partner_id
        if received_by_partner_id == sale.received_by_partner_id
        else _resolve_receiver(errors, user_id, received_by_partner_id)
    )
    if clean_qty is not None and clean_price is not None:
        _check_fits(errors, clean_qty, clean_price, sale.cost_each)
    errors.raise_if_any()

    money_changed = (
        clean_qty != sale.quantity
        or clean_price != sale.price_paid
        or receiver != sale.received_by_partner_id
    )
    if sale.settlement_id is not None and money_changed:
        db.session.rollback()
        raise LockedError(_LOCKED)

    sale.sale_date = checked(clean_day)
    sale.quantity = checked(clean_qty)
    sale.price_paid = checked(clean_price)
    sale.customer_name = clean_customer
    sale.notes = clean_notes
    sale.received_by_partner_id = receiver
    db.session.commit()
    return sale


def delete_sale(user_id: int, sale_id: int) -> None:
    """Soft-delete a sale, so it can be restored. Settled sales are locked."""
    sale = get_sale(user_id, sale_id, for_update=True)
    if sale.settlement_id is not None:
        db.session.rollback()
        raise LockedError("This sale is part of a settle-up. Undo the settle-up to delete it.")
    sale.deleted_at = datetime.now(UTC)
    db.session.commit()


def delete_sales(user_id: int, sale_ids: Iterable[int]) -> BulkDeleteResult:
    """Soft-delete many sales. Settled ones are skipped and reported, not treated as failures."""
    now = datetime.now(UTC)
    rows = db.session.scalars(
        select(Sale)
        .where(Sale.user_id == user_id, Sale.id.in_(set(sale_ids)), Sale.deleted_at.is_(None))
        .order_by(Sale.id)
        .with_for_update()
    ).all()
    deleted: list[int] = []
    locked: list[int] = []
    for sale in rows:
        if sale.settlement_id is None:
            sale.deleted_at = now
            deleted.append(sale.id)
        else:
            locked.append(sale.id)
    db.session.commit()
    return BulkDeleteResult(deleted_ids=tuple(deleted), locked_ids=tuple(locked))


def restore_sales(user_id: int, sale_ids: Iterable[int]) -> int:
    """Undo a delete. Returns how many sales came back."""
    rows = db.session.scalars(
        select(Sale)
        .where(Sale.user_id == user_id, Sale.id.in_(set(sale_ids)), Sale.deleted_at.is_not(None))
        .order_by(Sale.id)
        .with_for_update()
    ).all()
    for sale in rows:
        sale.deleted_at = None
    db.session.commit()
    return len(rows)


def list_sales(
    user_id: int,
    filters: SaleFilters | None = None,
    *,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> SalePage:
    """One page of matching sales, plus totals for everything that matched."""
    filters = filters or SaleFilters()
    per_page = min(max(1, per_page), MAX_PER_PAGE)
    conditions = _conditions(user_id, filters)

    totals = _totals(conditions)
    page_count = max(1, -(-totals.count // per_page))
    page = min(max(1, page), page_count)
    query = _ordered(select(Sale).where(*conditions), filters)
    items = db.session.scalars(query.limit(per_page).offset((page - 1) * per_page)).all()
    return SalePage(items=items, page=page, per_page=per_page, totals=totals)


def iter_sales(user_id: int, filters: SaleFilters | None = None) -> Sequence[Sale]:
    """Every matching sale without paging, e.g. for CSV export."""
    filters = filters or SaleFilters()
    return db.session.scalars(
        _ordered(select(Sale).where(*_conditions(user_id, filters)), filters)
    ).all()


def _conditions(user_id: int, filters: SaleFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Sale.user_id == user_id, Sale.deleted_at.is_(None)]
    if term := filters.search.strip():
        pattern = like_pattern(term)
        conditions.append(
            or_(
                Sale.customer_name.ilike(pattern, escape="\\"),
                Sale.product_name.ilike(pattern, escape="\\"),
            )
        )
    if filters.date_from is not None:
        conditions.append(Sale.sale_date >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(Sale.sale_date <= filters.date_to)
    if filters.product_id is not None:
        conditions.append(Sale.product_id == filters.product_id)
    if term := filters.customer.strip():
        conditions.append(Sale.customer_name.ilike(like_pattern(term), escape="\\"))
    if filters.received_by_partner_id is not None:
        conditions.append(Sale.received_by_partner_id == filters.received_by_partner_id)
    return conditions


def _ordered(query: Select[Sale], filters: SaleFilters) -> Select[Sale]:
    columns: dict[str, Any] = {
        "date": Sale.sale_date,
        "product": func.lower(Sale.product_name),
        "customer": func.lower(Sale.customer_name),
        "quantity": Sale.quantity,
        "price": Sale.price_paid,
        "profit": Sale.profit,
    }
    # An unknown sort falls back to the default: it comes straight from a URL.
    column = columns.get(filters.sort, Sale.sale_date)
    order = column.desc().nulls_last() if filters.descending else column.asc().nulls_last()
    tie_break = Sale.id.desc() if filters.descending else Sale.id.asc()
    return query.order_by(order, tie_break)


def _totals(conditions: list[ColumnElement[bool]]) -> SaleTotals:
    row = db.session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Sale.quantity), 0),
            func.coalesce(func.sum(Sale.price_paid), 0),
            func.coalesce(func.sum(Sale.total_cost), 0),
            func.coalesce(func.sum(Sale.profit), 0),
        ).where(*conditions)
    ).one()
    return SaleTotals(
        count=row[0],
        items_sold=int(row[1]),
        money_in=Decimal(row[2]),
        total_cost=Decimal(row[3]),
        profit=Decimal(row[4]),
    )


def _resolve_receiver(errors: Errors, user_id: int, partner_id: int | None) -> int | None:
    """Who the money went to: the person given, or else the account's usual receiver.

    The usual receiver is written onto the sale, so changing it later doesn't re-attribute
    sales already recorded.
    """
    if partner_id is None:
        return db.session.scalar(
            select(Partner.id).where(
                Partner.user_id == user_id,
                Partner.receives_money_default.is_(True),
                Partner.archived_at.is_(None),
            )
        )
    found = db.session.scalar(
        select(Partner.id).where(
            Partner.id == partner_id, Partner.user_id == user_id, Partner.archived_at.is_(None)
        )
    )
    if found is None:
        errors.add("received_by_partner_id", "Choose one of your people.")
    return found


def _check_fits(errors: Errors, quantity: int, price: Decimal, cost_each: Decimal) -> None:
    """Reject sales whose calculated columns wouldn't fit in the database."""
    if quantity * cost_each > MAX_AMOUNT:
        errors.add("quantity", "That quantity makes the total cost too large.")
    elif abs(price - quantity * cost_each) > MAX_AMOUNT:  # pragma: no cover - needs price > max
        errors.add("price_paid", "That amount is too large.")
