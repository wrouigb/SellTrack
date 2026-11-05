# Changelog

All notable changes to SellTrack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Exact money handling in Australian dollars (`app/money.py`). Amounts are `Decimal`, never
  floats; input like `NaN`, `Infinity`, `1e3` or non-ASCII digits is rejected; even splits always
  total exactly 100%.
- Profit split engine (`app/services/split_engine.py`): people with % shares, upfront costs paid
  back to whoever paid them, sale money tracked by who received it, part-payments, and suggested
  settle-up transfers that bring every balance to $0.
- Unit tests, Ruff, mypy (strict) and pytest configuration in `pyproject.toml`.
- The approved rebuild plan in `docs/plan.md`.

### Changed
- The original app moved to `legacy/` while it is rebuilt.
