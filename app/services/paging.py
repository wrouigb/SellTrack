"""One page of a query's results."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select

from app.extensions import db

DEFAULT_PER_PAGE = 25


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def has_previous(self) -> bool:
        return self.page > 1


def paginate(
    query: Select[*tuple[Any, ...]],
    *,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    rows: bool = False,
) -> Page[Any]:
    """Run ``query`` for one page. A page past the end shows the last one, as sales do.

    Items are the query's single entity, or whole rows with ``rows=True``.
    """
    total = (
        db.session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    )
    last_page = max(1, -(-total // per_page))
    page = min(max(1, page), last_page)
    limited = query.limit(per_page).offset((page - 1) * per_page)
    items = db.session.execute(limited).all() if rows else db.session.scalars(limited).all()
    return Page(items=items, page=page, per_page=per_page, total=total)
