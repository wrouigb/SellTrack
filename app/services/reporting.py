"""Figures for the dashboard. Everything is one SQL aggregate, not a loop over every sale.

The totals, the chart and the product list are cached for a few minutes: see
``app.figures_cache``, which also says why a write can't leave a wrong figure behind.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, Date, func, literal_column, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Mapped

from app import figures_cache
from app.extensions import db
from app.models import DEFAULT_TIMEZONE, Sale, UpfrontCost, User
from app.money import CENT
from app.services.errors import ValidationFailed

PERIOD_KEYS = ("this_month", "last_month", "last_30_days", "this_year", "all_time", "custom")
_ONE_DECIMAL = Decimal("0.1")
_DAILY_LIMIT_DAYS = 92
_ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class Period:
    """A date range, inclusive at both ends. ``None`` means unbounded."""

    key: str
    start: date | None
    end: date | None


@dataclass(frozen=True, slots=True)
class Totals:
    money_in: Decimal
    cost_of_items: Decimal
    profit: Decimal
    upfront_costs: Decimal
    items_sold: int
    sales_count: int

    @property
    def average_sale(self) -> Decimal:
        if not self.sales_count:
            return _ZERO
        return (self.money_in / self.sales_count).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Change:
    """How a figure moved against the previous period."""

    current: Decimal
    previous: Decimal
    amount: Decimal
    #: ``None`` when the previous period was zero, where a percentage means nothing.
    percent: Decimal | None


@dataclass(frozen=True, slots=True)
class Summary:
    period: Period
    previous_period: Period | None
    totals: Totals
    previous_totals: Totals | None
    changes: dict[str, Change]


@dataclass(frozen=True, slots=True)
class ProductFigures:
    name: str
    quantity: int
    money_in: Decimal
    profit: Decimal


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    day: date
    money_in: Decimal
    profit: Decimal


def today_for(user: User, *, now: datetime | None = None) -> date:
    """What date it is for this user, which is what "today" and "this month" mean to them."""
    moment = now or datetime.now(UTC)
    try:
        zone = ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return moment.astimezone(zone).date()


def resolve_period(
    key: str,
    today: date,
    *,
    start: date | None = None,
    end: date | None = None,
) -> Period:
    """Turn a choice from the period selector into actual dates."""
    if key == "this_month":
        return Period(key, today.replace(day=1), _month_end(today))
    if key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return Period(key, last.replace(day=1), last)
    if key == "last_30_days":
        return Period(key, today - timedelta(days=29), today)
    if key == "this_year":
        return Period(key, date(today.year, 1, 1), date(today.year, 12, 31))
    if key == "all_time":
        return Period(key, None, None)
    if key == "custom":
        if start is None or end is None:
            raise ValidationFailed(date_from="Choose both a start and an end date.")
        if start > end:
            raise ValidationFailed(date_to="The end date can't be before the start date.")
        return Period(key, start, end)
    raise ValidationFailed(period="That isn't a period you can choose.")


def previous_period(period: Period, *, today: date | None = None) -> Period | None:
    """The period to compare against: the one just before, the same length or calendar unit.

    Pass ``today`` to compare fairly while a month or year is still going. Five days into November
    is compared with the first five days of October, not all of it, which would look like a fall.
    """
    if period.start is None or period.end is None:
        return None
    in_progress = today is not None and period.start <= today < period.end
    if period.key in ("this_month", "last_month"):
        end = period.start - timedelta(days=1)
        start = end.replace(day=1)
        if in_progress and today is not None:
            end = start.replace(day=min(today.day, end.day))
        return Period("custom", start, end)
    if period.key == "this_year":
        year = period.start.year - 1
        end = date(year, 12, 31)
        if in_progress and today is not None:
            last_day = monthrange(year, today.month)[1]
            end = date(year, today.month, min(today.day, last_day))
        return Period("custom", date(year, 1, 1), end)
    length = period.end - period.start
    end = period.start - timedelta(days=1)
    return Period("custom", end - length, end)


def totals_for(user_id: int, period: Period) -> Totals:
    return figures_cache.remember(
        user_id,
        "totals",
        f"{period.start}:{period.end}",
        lambda: _work_out_totals(user_id, period),
        _encode_totals,
        _decode_totals,
    )


def _work_out_totals(user_id: int, period: Period) -> Totals:
    sales = db.session.execute(
        select(
            func.coalesce(func.sum(Sale.price_paid), 0),
            func.coalesce(func.sum(Sale.total_cost), 0),
            func.coalesce(func.sum(Sale.profit), 0),
            func.coalesce(func.sum(Sale.quantity), 0),
            func.count(),
        ).where(*_sale_conditions(user_id, period))
    ).one()
    upfront = db.session.scalar(
        select(func.coalesce(func.sum(UpfrontCost.amount), 0)).where(
            UpfrontCost.user_id == user_id,
            *_date_range(UpfrontCost.cost_date, period),
        )
    )
    return Totals(
        money_in=_money(sales[0]),
        cost_of_items=_money(sales[1]),
        profit=_money(sales[2]),
        items_sold=int(sales[3]),
        sales_count=sales[4],
        upfront_costs=_money(upfront),
    )


def summary(user_id: int, period: Period, *, today: date | None = None) -> Summary:
    """The dashboard tiles, each with its change against the previous period."""
    totals = totals_for(user_id, period)
    before = previous_period(period, today=today)
    previous_totals = totals_for(user_id, before) if before else None
    changes: dict[str, Change] = {}
    if previous_totals is not None:
        for name, current, previous in (
            ("money_in", totals.money_in, previous_totals.money_in),
            ("profit", totals.profit, previous_totals.profit),
            ("upfront_costs", totals.upfront_costs, previous_totals.upfront_costs),
            ("items_sold", Decimal(totals.items_sold), Decimal(previous_totals.items_sold)),
            ("sales_count", Decimal(totals.sales_count), Decimal(previous_totals.sales_count)),
            ("average_sale", totals.average_sale, previous_totals.average_sale),
        ):
            changes[name] = change_between(current, previous)
    return Summary(period, before, totals, previous_totals, changes)


def change_between(current: Decimal, previous: Decimal) -> Change:
    amount = current - previous
    percent = None
    if previous:
        percent = (amount * 100 / abs(previous)).quantize(_ONE_DECIMAL, ROUND_HALF_UP)
    return Change(current=current, previous=previous, amount=amount, percent=percent)


def by_product(user_id: int, period: Period, *, limit: int | None = None) -> list[ProductFigures]:
    """Profit by product, best first. Grouped by name, so removed products still show up."""
    return figures_cache.remember(
        user_id,
        "products",
        f"{period.start}:{period.end}:{limit}",
        lambda: _work_out_by_product(user_id, period, limit),
        _encode_products,
        _decode_products,
    )


def _work_out_by_product(user_id: int, period: Period, limit: int | None) -> list[ProductFigures]:
    query = (
        select(
            Sale.product_name,
            func.sum(Sale.quantity),
            func.sum(Sale.price_paid),
            func.sum(Sale.profit),
        )
        .where(*_sale_conditions(user_id, period))
        .group_by(Sale.product_name)
        .order_by(func.sum(Sale.profit).desc(), Sale.product_name)
    )
    if limit is not None:
        query = query.limit(limit)
    return [
        ProductFigures(
            name=row[0], quantity=int(row[1]), money_in=_money(row[2]), profit=_money(row[3])
        )
        for row in db.session.execute(query)
    ]


def series(
    user_id: int, period: Period, *, today: date | None = None
) -> tuple[Literal["day", "month"], list[SeriesPoint]]:
    """Money in and profit over time, for the chart: by day, or by month over longer ranges.

    Pass ``today`` to stop at today, unless a sale is dated later, so the chart isn't mostly the
    days that haven't happened yet.
    """
    return figures_cache.remember(
        user_id,
        "series",
        f"{period.start}:{period.end}:{today}",
        lambda: _work_out_series(user_id, period, today),
        _encode_series,
        _decode_series,
    )


def _work_out_series(
    user_id: int, period: Period, today: date | None
) -> tuple[Literal["day", "month"], list[SeriesPoint]]:
    first: date | None = period.start
    last: date | None = period.end
    if first is None or last is None:
        bounds = db.session.execute(
            select(func.min(Sale.sale_date), func.max(Sale.sale_date)).where(
                *_sale_conditions(user_id, period)
            )
        ).one()
        # min() and max() are NULL when nothing matches, whatever the column's type says.
        first = first or cast("date | None", bounds[0])
        last = last or cast("date | None", bounds[1])
    if first is None or last is None:  # no sales at all
        return "day", []
    if today is not None and first <= today < last:
        latest_sale = db.session.scalar(
            select(func.max(Sale.sale_date)).where(*_sale_conditions(user_id, period))
        )
        last = max(today, latest_sale) if latest_sale else today

    bucket: Literal["day", "month"] = "day" if (last - first).days <= _DAILY_LIMIT_DAYS else "month"
    # A literal rather than a bound parameter: Postgres has to see the SELECT and GROUP BY
    # expressions as the same thing.
    column = (
        Sale.sale_date
        if bucket == "day"
        else sql_cast(func.date_trunc(literal_column("'month'"), Sale.sale_date), Date)
    )
    rows = db.session.execute(
        select(column, func.sum(Sale.price_paid), func.sum(Sale.profit))
        .where(*_sale_conditions(user_id, period))
        .group_by(column)
    )
    found = {row[0]: (_money(row[1]), _money(row[2])) for row in rows}

    # Fill the gaps, so a day with no sales shows as zero rather than vanishing from the chart.
    points: list[SeriesPoint] = []
    cursor = first if bucket == "day" else first.replace(day=1)
    while cursor <= last:
        money_in, profit = found.get(cursor, (_ZERO, _ZERO))
        points.append(SeriesPoint(day=cursor, money_in=money_in, profit=profit))
        cursor = cursor + timedelta(days=1) if bucket == "day" else _next_month(cursor)
    return bucket, points


# The cache holds JSON, which has no decimals or dates, so amounts and days travel as text.


def _encode_totals(totals: Totals) -> list[object]:
    return [
        str(totals.money_in),
        str(totals.cost_of_items),
        str(totals.profit),
        str(totals.upfront_costs),
        totals.items_sold,
        totals.sales_count,
    ]


def _decode_totals(saved: list[Any]) -> Totals:
    money_in, cost_of_items, profit, upfront_costs, items_sold, sales_count = saved
    return Totals(
        money_in=Decimal(money_in),
        cost_of_items=Decimal(cost_of_items),
        profit=Decimal(profit),
        upfront_costs=Decimal(upfront_costs),
        items_sold=items_sold,
        sales_count=sales_count,
    )


def _encode_products(rows: list[ProductFigures]) -> list[list[object]]:
    return [[row.name, row.quantity, str(row.money_in), str(row.profit)] for row in rows]


def _decode_products(saved: list[list[Any]]) -> list[ProductFigures]:
    return [
        ProductFigures(
            name=name, quantity=quantity, money_in=Decimal(money), profit=Decimal(profit)
        )
        for name, quantity, money, profit in saved
    ]


def _encode_series(
    value: tuple[Literal["day", "month"], list[SeriesPoint]],
) -> list[object]:
    bucket, points = value
    return [
        bucket,
        [[point.day.isoformat(), str(point.money_in), str(point.profit)] for point in points],
    ]


def _decode_series(saved: list[Any]) -> tuple[Literal["day", "month"], list[SeriesPoint]]:
    bucket, points = saved
    return bucket, [
        SeriesPoint(day=date.fromisoformat(day), money_in=Decimal(money), profit=Decimal(profit))
        for day, money, profit in points
    ]


def recent_sales(user_id: int, *, limit: int = 5) -> Sequence[Sale]:
    return db.session.scalars(
        select(Sale)
        .where(Sale.user_id == user_id, Sale.deleted_at.is_(None))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(limit)
    ).all()


def _sale_conditions(user_id: int, period: Period) -> list[ColumnElement[bool]]:
    return [
        Sale.user_id == user_id,
        Sale.deleted_at.is_(None),
        *_date_range(Sale.sale_date, period),
    ]


def _date_range(column: Mapped[date], period: Period) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if period.start is not None:
        conditions.append(column >= period.start)
    if period.end is not None:
        conditions.append(column <= period.end)
    return conditions


def _money(value: Decimal | int | None) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def _next_month(first_of_month: date) -> date:
    return _month_end(first_of_month) + timedelta(days=1)
