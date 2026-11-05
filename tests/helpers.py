"""Small helpers shared by the tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.extensions import db


@contextmanager
def count_queries() -> Iterator[list[str]]:
    """Collect every SQL statement run inside the block."""
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
