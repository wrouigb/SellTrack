"""The chart's geometry and wording."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.charts import VIEW_HEIGHT, VIEW_WIDTH, _axis_money, _nice_step, build_chart
from app.services.reporting import SeriesPoint


def point(day: int, money: str, profit: str) -> SeriesPoint:
    return SeriesPoint(day=date(2026, 11, day), money_in=Decimal(money), profit=Decimal(profit))


def test_nothing_to_draw():
    assert build_chart("day", []) is None
    assert build_chart("day", [point(1, "0.00", "0.00"), point(2, "0.00", "0.00")]) is None


def test_bands_fill_the_width_evenly():
    chart = build_chart("day", [point(day, "10.00", "5.00") for day in range(1, 5)])
    assert [band.x for band in chart.bands] == [0, 250, 500, 750]
    assert all(band.width == VIEW_WIDTH / 4 for band in chart.bands)


def test_a_day_with_no_sales_draws_no_bars_but_can_still_be_pointed_at():
    chart = build_chart("day", [point(1, "10.00", "5.00"), point(2, "0.00", "0.00")])
    empty = chart.bands[1]
    assert empty.money_path is None
    assert empty.profit_path is None
    assert empty.money_text == "$0.00"


def test_axis_starts_at_zero_and_tops_out_at_a_clean_number():
    chart = build_chart("day", [point(1, "37.50", "20.00")])
    assert chart.tick_labels == ("$40", "$30", "$20", "$10", "$0")
    assert chart.baseline_y == VIEW_HEIGHT
    assert chart.grid_ys[0] == 0


def test_a_loss_extends_the_axis_below_zero_and_hangs_below_the_line():
    chart = build_chart("day", [point(1, "10.00", "-10.00")])
    assert chart.tick_labels[-1] == "−$10"
    assert 0 < chart.baseline_y < VIEW_HEIGHT
    path = chart.bands[0].profit_path
    tip = float(path.split("Q")[1].split()[1])
    assert tip > chart.baseline_y


def test_bars_stay_thin_when_there_are_few_of_them():
    """One huge block would be loud, so bars are capped however wide their band is."""
    chart = build_chart("day", [point(1, "10.00", "5.00")])
    path = chart.bands[0].money_path
    numbers = [float(n) for n in path.replace("M", " ").replace("L", " ").split()[:2]]
    left = numbers[0]
    right = float(path.split("L")[-1].split()[0])
    assert right - left <= 23


def test_every_value_is_in_full_for_the_tooltip_and_table():
    chart = build_chart("day", [point(5, "1234.50", "-3.00")])
    band = chart.bands[0]
    assert band.label == "5 November 2026"
    assert band.money_text == "$1,234.50"
    assert band.profit_text == "−$3.00"


def test_dates_along_the_bottom():
    points = [point(day, "1.00", "1.00") for day in range(1, 8)]
    assert build_chart("day", points).x_labels == ("1 Nov", "4 Nov", "7 Nov")
    assert build_chart("day", points[:2]).x_labels == ("1 Nov", "2 Nov")
    assert build_chart("day", points[:1]).x_labels == ("1 Nov",)
    assert build_chart("month", points[:1]).x_labels == ("Nov 2026",)


def test_the_description_says_what_the_chart_shows():
    chart = build_chart("day", [point(1, "10.00", "5.00"), point(2, "30.00", "12.00")])
    assert "$40.00 came in" in chart.description
    assert "$17.00 profit" in chart.description
    assert "2 November 2026" in chart.description


def test_steps_are_round_numbers():
    assert _nice_step(Decimal("3")) == 5
    assert _nice_step(Decimal("12")) == 20
    assert _nice_step(Decimal("0.3")) == Decimal("0.5")
    assert _nice_step(Decimal("95")) == 100


def test_axis_labels_stay_short():
    assert _axis_money(Decimal("0")) == "$0"
    assert _axis_money(Decimal("2500")) == "$2,500"
    assert _axis_money(Decimal("20000")) == "$20K"
    assert _axis_money(Decimal("12500")) == "$12.5K"
    assert _axis_money(Decimal("-500")) == "−$500"
    assert _axis_money(Decimal("0.50")) == "$0.50"
