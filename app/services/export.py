"""CSV downloads of your own data."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator, Mapping

from app.models import Sale

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
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str | None) -> str:
    """Text for a CSV cell that a spreadsheet will show rather than run."""
    text = value or ""
    return f"'{text}" if text.startswith(_FORMULA_STARTERS) else text


def sales_csv(sales: Iterable[Sale], people: Mapping[int, str]) -> Iterator[str]:
    """The rows of a CSV file, one string at a time. Amounts are plain numbers, like 1234.50."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def line(row: Iterable[object]) -> str:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        return buffer.getvalue()

    yield line(SALES_HEADER)
    for sale in sales:
        receiver = people.get(sale.received_by_partner_id) if sale.received_by_partner_id else None
        yield line(
            (
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
        )
