"""Works out the geometry of the money-in and profit chart.

The chart is plain SVG drawn on the server, so it needs no JavaScript and nothing the Content
Security Policy would block. It stretches to fit its container, so numbers are drawn as ordinary
HTML beside it, which keeps them readable at any width.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil, floor

from app.money import format_aud
from app.services.reporting import SeriesPoint

VIEW_WIDTH = 1000
VIEW_HEIGHT = 300
_TICKS = 4
#: Bars are capped at about 24px on the widest layout. The viewBox is roughly 1px per unit there.
_MAX_BAR = 23.0
_BAR_SHARE = 0.38
_GAP_SHARE = 0.04
_COMPACT_FROM = 10_000


@dataclass(frozen=True, slots=True)
class Band:
    """One day or month: its two bars, and the area that responds to the pointer."""

    x: float
    width: float
    label: str
    money_text: str
    profit_text: str
    money_path: str | None
    profit_path: str | None


@dataclass(frozen=True, slots=True)
class Chart:
    bands: tuple[Band, ...]
    #: Values for the left axis, from top to bottom.
    tick_labels: tuple[str, ...]
    grid_ys: tuple[float, ...]
    baseline_y: float
    #: Dates along the bottom.
    x_labels: tuple[str, ...]
    description: str


def build_chart(bucket: str, points: list[SeriesPoint]) -> Chart | None:
    """Lay out the chart, or return ``None`` when nothing happened in the period."""
    if not any(point.money_in or point.profit for point in points):
        return None

    highest = max(max(point.money_in, point.profit) for point in points)
    lowest = min(Decimal(0), min(point.profit for point in points))
    step = _nice_step((highest - lowest) / _TICKS)
    top = Decimal(ceil(highest / step)) * step
    bottom = Decimal(floor(lowest / step)) * step
    if top == bottom:  # pragma: no cover - only reachable if every value were zero
        top = bottom + step

    def y_of(value: Decimal) -> float:
        return float((top - value) / (top - bottom)) * VIEW_HEIGHT

    ticks = []
    value = top
    while value >= bottom:
        ticks.append(value)
        value -= step

    baseline = y_of(Decimal(0))
    band_width = VIEW_WIDTH / len(points)
    bar = min(band_width * _BAR_SHARE, _MAX_BAR)
    gap = max(band_width * _GAP_SHARE, 2.0)
    bands = []
    for index, point in enumerate(points):
        x = index * band_width
        start = x + (band_width - (2 * bar + gap)) / 2
        bands.append(
            Band(
                x=round(x, 2),
                width=round(band_width, 2),
                label=_point_label(bucket, point),
                money_text=format_aud(point.money_in),
                profit_text=format_aud(point.profit),
                money_path=_column(start, bar, baseline, y_of(point.money_in)),
                profit_path=_column(start + bar + gap, bar, baseline, y_of(point.profit)),
            )
        )

    return Chart(
        bands=tuple(bands),
        tick_labels=tuple(_axis_money(tick) for tick in ticks),
        grid_ys=tuple(round(y_of(tick), 2) for tick in ticks),
        baseline_y=round(baseline, 2),
        x_labels=_x_labels(bucket, points),
        description=_describe(bucket, points),
    )


def _column(x: float, width: float, baseline: float, tip: float) -> str | None:
    """A bar that's rounded at its data end and square on the baseline."""
    height = abs(baseline - tip)
    if height < 0.01:
        return None
    direction = 1 if tip < baseline else -1  # bars for losses hang below the line
    rx = min(width / 3, 4.0)
    ry = min(height, 5.0)
    shoulder = tip + direction * ry
    return (
        f"M{x:.2f} {baseline:.2f}"
        f"L{x:.2f} {shoulder:.2f}"
        f"Q{x:.2f} {tip:.2f} {x + rx:.2f} {tip:.2f}"
        f"L{x + width - rx:.2f} {tip:.2f}"
        f"Q{x + width:.2f} {tip:.2f} {x + width:.2f} {shoulder:.2f}"
        f"L{x + width:.2f} {baseline:.2f}Z"
    )


def _nice_step(rough: Decimal) -> Decimal:
    """Round a step up to 1, 2 or 5 times a power of ten, so the axis has clean numbers."""
    if rough <= 0:  # pragma: no cover - only reachable if every value were zero
        return Decimal(1)
    magnitude = Decimal(10) ** (rough.adjusted())
    for multiple in (1, 2, 5, 10):
        step = magnitude * multiple
        if step >= rough:
            return step
    raise AssertionError("10 times the magnitude is always large enough")  # pragma: no cover


def _axis_money(value: Decimal) -> str:
    sign = "−" if value < 0 else ""
    size = abs(value)
    if size >= _COMPACT_FROM:
        thousands = size / 1000
        text = f"{thousands:,.0f}" if thousands == thousands.to_integral() else f"{thousands:,.1f}"
        return f"{sign}${text}K"
    if size == size.to_integral():
        return f"{sign}${size:,.0f}"
    return f"{sign}${size:,.2f}"


def _point_label(bucket: str, point: SeriesPoint) -> str:
    if bucket == "month":
        return f"{point.day:%B %Y}"
    return f"{point.day.day} {point.day:%B %Y}"


def _short_label(bucket: str, point: SeriesPoint) -> str:
    if bucket == "month":
        return f"{point.day:%b %Y}"
    return f"{point.day.day} {point.day:%b}"


def _x_labels(bucket: str, points: list[SeriesPoint]) -> tuple[str, ...]:
    if len(points) <= 2:
        return tuple(_short_label(bucket, point) for point in points)
    middle = points[len(points) // 2]
    return (
        _short_label(bucket, points[0]),
        _short_label(bucket, middle),
        _short_label(bucket, points[-1]),
    )


def _describe(bucket: str, points: list[SeriesPoint]) -> str:
    busiest = max(points, key=lambda point: point.money_in)
    total_in = sum((point.money_in for point in points), Decimal(0))
    total_profit = sum((point.profit for point in points), Decimal(0))
    return (
        f"Column chart of money in and profit by {bucket}. "
        f"In total, {format_aud(total_in)} came in "
        f"with {format_aud(total_profit)} profit. The most money came in on "
        f"{_point_label(bucket, busiest)}, at {format_aud(busiest.money_in)}."
    )
