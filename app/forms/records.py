"""Forms for sales, products and the split."""

from __future__ import annotations

from collections.abc import Iterable

from flask_wtf import FlaskForm
from wtforms import BooleanField, SearchField, SelectField, StringField, TextAreaField
from wtforms.fields.choices import SelectChoice
from wtforms.widgets import DateInput, NumberInput

from app.models import CostCategory, Theme
from app.services.auth import timezone_names
from app.services.products import ProductRow

_CATEGORY_LABELS = {
    CostCategory.STOCK: "Stock",
    CostCategory.EQUIPMENT: "Equipment",
    CostCategory.FEES: "Fees",
    CostCategory.PACKAGING: "Packaging",
    CostCategory.OTHER: "Other",
}
CATEGORY_CHOICES = [SelectChoice(c.value, label) for c, label in _CATEGORY_LABELS.items()]


def options(pairs: Iterable[tuple[object, str]]) -> list[SelectChoice]:
    """Dropdown options from pairs of value and label."""
    return [SelectChoice(str(value), label) for value, label in pairs]


def timezone_options() -> list[SelectChoice]:
    """Time zones grouped in the dropdown, with Australia's first."""
    return [
        SelectChoice(
            zone,
            zone.replace("_", " ").replace("/", ": ", 1),
            optgroup="Australia" if zone.startswith("Australia/") else "Elsewhere",
        )
        for zone in timezone_names()
    ]


def product_options(rows: Iterable[ProductRow]) -> list[SelectChoice]:
    """Products for the sale form. The page reads each one's cost and price to fill in figures."""
    return [
        SelectChoice("", "Choose a product"),
        *(
            SelectChoice(
                str(row.product.id),
                row.product.name,
                {
                    "data-cost": str(row.product.cost),
                    "data-price": (
                        "" if row.product.sell_price is None else str(row.product.sell_price)
                    ),
                },
            )
            for row in rows
        ),
    ]


def category_label(value: str) -> str:
    return _CATEGORY_LABELS[CostCategory(value)]


class SaleForm(FlaskForm):
    product_id = SelectField("Product", choices=[], validate_choice=False)
    sale_date = StringField("Date", widget=DateInput())
    quantity = StringField(
        "Quantity", widget=NumberInput(min=1, step=1), render_kw={"inputmode": "numeric"}
    )
    price_paid = StringField("Total paid")
    customer_name = StringField("Customer")
    received_by_partner_id = SelectField(
        "Who received the money", choices=[], validate_choice=False
    )
    notes = TextAreaField("Notes", render_kw={"rows": 2})


class SaleFilterForm(FlaskForm):
    class Meta:
        csrf = False

    q = SearchField("Search customer or product")
    date_from = StringField("From", widget=DateInput())
    date_to = StringField("To", widget=DateInput())
    product = SelectField("Product", choices=[], validate_choice=False)
    received_by = SelectField("Received by", choices=[], validate_choice=False)


class SupportForm(FlaskForm):
    subject = StringField("Subject")
    body = TextAreaField("Message")


class ProductForm(FlaskForm):
    name = StringField("Name")
    cost = StringField("Cost per item")
    sell_price = StringField("Selling price")


class ProductSearchForm(FlaskForm):
    class Meta:
        csrf = False

    q = SearchField("Search products")
    archived = BooleanField("Show archived products")


class PersonForm(FlaskForm):
    name = StringField("Name")
    share_pct = StringField("Share (%)")
    usually_receives_money = BooleanField("This person usually receives the sale money")


class CostForm(FlaskForm):
    description = StringField("What was it for?")
    category = SelectField("Category", choices=CATEGORY_CHOICES, validate_choice=False)
    amount = StringField("Amount")
    cost_date = StringField("Date", widget=DateInput())
    paid_by_partner_id = SelectField("Who paid?", choices=[], validate_choice=False)
    product_id = SelectField("For a product (optional)", choices=[], validate_choice=False)
    notes = TextAreaField("Notes", render_kw={"rows": 2})


class PaymentForm(FlaskForm):
    from_partner_id = SelectField("Who paid?", choices=[], validate_choice=False)
    to_partner_id = SelectField("Who did they pay?", choices=[], validate_choice=False)
    amount = StringField("Amount")
    paid_on = StringField("Date", widget=DateInput())
    note = StringField("Note (optional)")


class PeriodForm(FlaskForm):
    class Meta:
        csrf = False

    period = SelectField(
        "Period",
        choices=options(
            [
                ("this_month", "This month"),
                ("last_month", "Last month"),
                ("last_30_days", "Last 30 days"),
                ("this_year", "This year"),
                ("all_time", "All time"),
                ("custom", "Choose dates"),
            ]
        ),
        validate_choice=False,
    )
    date_from = StringField("From", widget=DateInput())
    date_to = StringField("To", widget=DateInput())


class PreferencesForm(FlaskForm):
    theme = SelectField(
        "Appearance",
        choices=options(
            [
                (Theme.SYSTEM.value, "Match this device"),
                (Theme.LIGHT.value, "Light"),
                (Theme.DARK.value, "Dark"),
            ]
        ),
        validate_choice=False,
    )
    timezone = SelectField("Time zone", choices=[], validate_choice=False)


class TimezoneForm(FlaskForm):
    timezone = SelectField("Time zone", choices=[], validate_choice=False)
