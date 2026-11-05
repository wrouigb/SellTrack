# SellTrack

Track what you sell, see what you make, and split the profit fairly.

> **Status: being rebuilt for production.** This branch is phase 3 of the plan in
> [`docs/plan.md`](docs/plan.md). The original Flask/SQLite app is kept in
> [`legacy/`](legacy/) as the reference for the new pages and is removed at the end of phase 4.

## What's here so far

| Path | What it is |
|---|---|
| `app/money.py` | Exact AUD money handling: strict parsing, cents, en-AU formatting, splitting totals without losing a cent |
| `app/services/split_engine.py` | The profit split rules: upfront costs paid back first, profit shared by %, who owes whom, settle-up transfers |
| `tests/unit/` | Unit tests for both, including randomised checks that balances always total $0 |

## Checks

```bash
python -m pytest
ruff check . && ruff format --check .
mypy app
```

Full setup, configuration, Docker and deployment instructions arrive with the rest of phase 3 and phase 6.
