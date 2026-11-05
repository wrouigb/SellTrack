"""Downloads of your own data: the sales CSV, and a zip of everything in the account."""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime
from typing import IO, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import (
    Notification,
    Partner,
    PartnerPayment,
    Product,
    ProductCostChange,
    Sale,
    Settlement,
    SignIn,
    SupportMessage,
    UpfrontCost,
    User,
)
from app.services import audit

#: Excel only reads a CSV as UTF-8 when it starts with this. Without it, "José" comes out garbled.
BOM = "\ufeff"
SALES_HEADER = (
    "Date",
    "Product",
    "Customer",
    "Quantity",
    "Cost each",
    "Total cost",
    "Total paid",
    "Profit",
    "Received by",
    "Notes",
    "Settled",
)
# Spreadsheets run a cell starting with one of these as a formula, so a customer name like
# =HYPERLINK(...) could do something nasty when the file is opened.
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r", "\n")
#: Rows fetched from the database at a time, so a big account doesn't have to fit in memory.
_BATCH = 500

Table = tuple[str, tuple[str, ...], Iterable[Iterable[object]]]


def safe_text(value: str | None) -> str:
    """Text for a CSV cell that a spreadsheet will show rather than run."""
    text = value or ""
    return f"'{text}" if text.lstrip(" ").startswith(_FORMULA_STARTERS) else text


def sales_csv(sales: Iterable[Sale], people: Mapping[int, str]) -> Iterator[str]:
    """The rows of a CSV file, one string at a time. Amounts are plain numbers, like 1234.50."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def line(row: Iterable[object]) -> str:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        return buffer.getvalue()

    yield BOM + line(SALES_HEADER)
    for sale in sales:
        yield line(_sale_row(sale, people))


def _sale_row(sale: Sale, people: Mapping[int, str]) -> tuple[object, ...]:
    receiver = people.get(sale.received_by_partner_id) if sale.received_by_partner_id else None
    return (
        sale.sale_date.isoformat(),
        safe_text(sale.product_name),
        safe_text(sale.customer_name),
        sale.quantity,
        sale.cost_each,
        sale.total_cost,
        sale.price_paid,
        sale.profit,
        safe_text(receiver),
        safe_text(sale.notes),
        "Yes" if sale.settlement_id else "No",
    )


def account_zip(user_id: int) -> tuple[IO[bytes], int]:
    """A zip of CSV files holding everything in the account, and its size in bytes.

    It's built in a temporary file, which disappears when closed, so a large account doesn't
    have to fit in a Raspberry Pi's memory. All of it comes from one snapshot of the database:
    someone settling up halfway through can't make the files disagree with each other.
    Passwords and recovery codes are never included, and neither are sales waiting to be purged.
    """
    archive = tempfile.TemporaryFile()  # noqa: SIM115 - the response closes it
    connection_options = {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    try:
        with (
            db.engine.connect().execution_options(**connection_options) as connection,
            Session(connection) as session,
            zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle,
        ):
            for name, header, rows in _tables(session, user_id):
                _write_csv(bundle, name, header, rows)
        size = archive.tell()
        archive.seek(0)
        audit.record("data.exported", actor_id=user_id, target_type="user", target_id=user_id)
        db.session.commit()
    except BaseException:
        archive.close()
        raise
    return archive, size


def _write_csv(
    bundle: zipfile.ZipFile, name: str, header: Iterable[str], rows: Iterable[Iterable[object]]
) -> None:
    with (
        bundle.open(name, "w") as raw,
        io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
    ):
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerows(rows)


def _tables(session: Session, user_id: int) -> Iterator[Table]:
    people = session.scalars(
        select(Partner).where(Partner.user_id == user_id).order_by(Partner.id)
    ).all()
    names = {person.id: person.name for person in people}
    products = session.scalars(
        select(Product).where(Product.user_id == user_id).order_by(Product.name, Product.id)
    ).all()
    product_names = {product.id: product.name for product in products}

    def rows_of(model: Any, *order: Any, **conditions: Any) -> Any:
        return session.scalars(
            select(model)
            .filter_by(user_id=user_id, **conditions)
            .order_by(*order)
            .execution_options(yield_per=_BATCH)
        )

    user = session.get_one(User, user_id)
    yield (
        "account.csv",
        ("Username", "Joined", "Time zone", "Appearance", "Last login"),
        [
            (
                safe_text(user.username),
                _moment(user.created_at),
                user.timezone,
                user.theme.value,
                _moment(user.last_login_at),
            )
        ],
    )
    yield (
        "people.csv",
        ("Name", "Share (%)", "Usually receives the money", "Added", "Archived"),
        (
            (
                safe_text(person.name),
                person.share_pct,
                "Yes" if person.receives_money_default else "No",
                _moment(person.created_at),
                _moment(person.archived_at),
            )
            for person in people
        ),
    )
    yield (
        "products.csv",
        ("Name", "Cost each", "Selling price", "Added", "Archived"),
        (
            (
                safe_text(product.name),
                product.cost,
                "" if product.sell_price is None else product.sell_price,
                _moment(product.created_at),
                _moment(product.archived_at),
            )
            for product in products
        ),
    )
    yield (
        "product_cost_changes.csv",
        ("Product", "Old cost", "New cost", "Changed"),
        (
            (
                safe_text(product_names[change.product_id]),
                "" if change.old_cost is None else change.old_cost,
                change.new_cost,
                _moment(change.changed_at),
            )
            for change in session.scalars(
                select(ProductCostChange)
                .join(Product, ProductCostChange.product_id == Product.id)
                .where(Product.user_id == user_id)
                .order_by(ProductCostChange.changed_at, ProductCostChange.id)
                .execution_options(yield_per=_BATCH)
            )
        ),
    )
    yield (
        "sales.csv",
        SALES_HEADER,
        (
            _sale_row(sale, names)
            for sale in session.scalars(
                select(Sale)
                .where(Sale.user_id == user_id, Sale.deleted_at.is_(None))
                .order_by(Sale.sale_date, Sale.id)
                .execution_options(yield_per=_BATCH)
            )
        ),
    )
    yield (
        "upfront_costs.csv",
        ("Date", "Description", "Category", "Amount", "Paid by", "Product", "Notes", "Settled"),
        (
            (
                _day(cost.cost_date),
                safe_text(cost.description),
                cost.category.value,
                cost.amount,
                safe_text(names.get(cost.paid_by_partner_id)),
                safe_text(product_names.get(cost.product_id) if cost.product_id else None),
                safe_text(cost.notes),
                "Yes" if cost.settlement_id else "No",
            )
            for cost in rows_of(UpfrontCost, UpfrontCost.cost_date, UpfrontCost.id)
        ),
    )
    yield (
        "payments.csv",
        ("Date", "From", "To", "Amount", "Note", "Settled"),
        (
            (
                _day(payment.paid_on),
                safe_text(names.get(payment.from_partner_id)),
                safe_text(names.get(payment.to_partner_id)),
                payment.amount,
                safe_text(payment.note),
                "Yes" if payment.settlement_id else "No",
            )
            for payment in rows_of(PartnerPayment, PartnerPayment.paid_on, PartnerPayment.id)
        ),
    )
    settlements = session.scalars(
        select(Settlement)
        .where(Settlement.user_id == user_id)
        .order_by(Settlement.settled_at, Settlement.id)
    ).all()
    yield (
        "settle_ups.csv",
        ("Settled", "Period start", "Period end", "Money in", "Upfront costs", "Profit shared"),
        (
            (
                _moment(settlement.settled_at),
                _day(settlement.period_start),
                _day(settlement.period_end),
                settlement.money_in,
                settlement.upfront_total,
                settlement.profit_to_share,
            )
            for settlement in settlements
        ),
    )
    yield (
        "settle_up_people.csv",
        (
            "Settled",
            "Person",
            "Share (%)",
            "Gets back",
            "Share of profit",
            "Should end with",
            "Holding",
            "Balance",
        ),
        (
            (
                _moment(settlement.settled_at),
                safe_text(person["name"]),
                person["share_pct"],
                person["gets_back"],
                person["share_of_profit"],
                person["should_end_with"],
                person["holding"],
                person["balance"],
            )
            for settlement in settlements
            for person in settlement.snapshot["people"]
        ),
    )
    yield (
        "support_messages.csv",
        ("Sent", "Subject", "Message", "Status", "Reply", "Replied"),
        (
            (
                _moment(message.created_at),
                safe_text(message.subject),
                safe_text(message.body),
                message.status.value,
                safe_text(message.admin_reply),
                _moment(message.replied_at),
            )
            for message in rows_of(SupportMessage, SupportMessage.created_at, SupportMessage.id)
        ),
    )
    yield (
        "notifications.csv",
        ("Sent", "Title", "Message", "Read"),
        (
            (
                _moment(note.created_at),
                safe_text(note.title),
                safe_text(note.body),
                _moment(note.read_at),
            )
            for note in rows_of(Notification, Notification.created_at, Notification.id)
        ),
    )
    yield (
        "sign_ins.csv",
        ("When", "Address", "Device"),
        (
            (_moment(sign_in.created_at), sign_in.ip or "", safe_text(sign_in.user_agent))
            for sign_in in rows_of(SignIn, SignIn.created_at, SignIn.id)
        ),
    )


def _moment(value: datetime | None) -> str:
    """A moment in time, in UTC, in a form that spreadsheets and other software understand."""
    return value.isoformat() if value is not None else ""


def _day(value: date) -> str:
    return value.isoformat()
