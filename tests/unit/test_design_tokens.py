"""Every colour used for text is readable, in both themes (WCAG 2.2 AA, plan section 8)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parents[2] / "app/static/src/app.css").read_text()
AA_TEXT = 4.5
AA_GRAPHICS = 3.0

#: Each piece of text and the backgrounds it's actually shown on.
TEXT_PAIRS = [
    ("ink", "bg"),
    ("ink", "surface"),
    ("ink", "surface-sunken"),
    ("ink-soft", "bg"),
    ("ink-soft", "surface"),
    ("ink-soft", "surface-sunken"),
    ("primary", "bg"),  # links
    ("primary", "surface"),
    ("primary", "surface-sunken"),
    ("on-primary", "primary"),
    ("on-primary", "primary-hover"),
    ("on-band", "band"),
    ("on-band-soft", "band"),
    ("on-band", "band-active"),
    ("on-tag", "tag"),
    ("on-tag-soft", "tag"),
    ("on-tag", "tag-edge"),  # the header's Add sale button, on hover
    ("success", "success-bg"),
    ("success", "surface"),
    ("danger", "danger-bg"),
    ("danger", "surface"),
    ("danger", "bg"),
    ("warning", "warning-bg"),
    ("info", "info-bg"),
    ("ink", "success-bg"),
    ("ink", "danger-bg"),
    ("ink", "warning-bg"),
    ("ink", "info-bg"),
]
#: Control outlines, focus rings and chart marks only need 3:1.
GRAPHIC_PAIRS = [
    ("line-strong", "surface"),
    ("line-strong", "bg"),
    ("focus", "bg"),
    ("focus", "surface"),
    ("danger", "surface"),
    ("chart-money", "surface"),
    ("chart-profit", "surface"),
]


def tokens(theme: str) -> dict[str, str]:
    block = re.search(rf"/\* tokens:{theme} \*/(.*?)/\* end tokens:{theme} \*/", CSS, re.S)
    assert block, f"no {theme} tokens found"
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", block.group(1)))


def luminance(colour: str) -> float:
    def channel(position: int) -> float:
        value = int(colour[position : position + 2], 16) / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_formula_matches_known_values():
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("text", "background"), TEXT_PAIRS)
def test_text_is_readable(theme, text, background):
    colours = tokens(theme)
    ratio = contrast(colours[text], colours[background])
    assert ratio >= AA_TEXT, f"{text} on {background} is {ratio:.2f}:1 in {theme}"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("graphic", "background"), GRAPHIC_PAIRS)
def test_outlines_and_chart_marks_are_visible(theme, graphic, background):
    colours = tokens(theme)
    ratio = contrast(colours[graphic], colours[background])
    assert ratio >= AA_GRAPHICS, f"{graphic} on {background} is {ratio:.2f}:1 in {theme}"


def test_both_themes_define_the_same_tokens():
    assert set(tokens("light")) == set(tokens("dark"))


def test_the_dark_theme_is_the_same_whichever_way_it_was_chosen():
    """Following the device and choosing dark in settings are written out separately in the CSS."""
    forced = re.search(r':root\[data-theme="dark"\] \{(.*?)\}', CSS, re.S)
    assert forced
    values = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6});", forced.group(1)))
    assert values == tokens("dark")


def test_the_original_brand_colours_are_kept():
    light = tokens("light")
    assert light["primary"] == "#1f4e79"
    assert light["bg"] == "#f5f0e6"
