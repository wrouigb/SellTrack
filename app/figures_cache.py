"""Dashboard figures, kept in Redis for a few minutes.

Each key includes a generation number for the account. A commit that changes the account's sales,
costs or products adds one to it, and the old figures simply can't be asked for again. That's why
this watches the session rather than relying on every service remembering to clear the cache: a
write that forgets can't leave a wrong figure behind.

The order matters, and it's what keeps this right when requests overlap. The generation is read
before the figures are worked out, and it changes only after the commit. So figures worked out
from data that's about to change are filed under the old number, where nobody will look for them.

Only the dashboard's own totals are cached. Who owes whom is always worked out fresh.

Redis being away makes the dashboard slower, not wrong: everything falls back to the database. If
Redis misses a change because it was away, the figures last at most ``CACHE_DEFAULT_TIMEOUT``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import InvalidOperation
from itertools import chain
from typing import Any

import redis
import structlog
from flask_caching.backends.base import BaseCache
from sqlalchemy import event
from sqlalchemy.orm import Session, UOWTransaction

from app.extensions import cache
from app.models import Product, Sale, UpfrontCost

log = structlog.get_logger("selltrack.cache")

#: Whatever a dashboard total is worked out from. Renaming a product renames its sales too.
_WATCHED = (Sale, UpfrontCost, Product)
_CHANGED_ACCOUNTS = "figures_changed_accounts"
#: ``ValueError`` is what's left of something in Redis that isn't JSON.
_CACHE_TROUBLE = (redis.RedisError, OSError, ValueError)
_BAD_ENTRY = (KeyError, IndexError, TypeError, ValueError, InvalidOperation)


def remember[T](
    user_id: int,
    name: str,
    params: str,
    compute: Callable[[], T],
    encode: Callable[[T], Any],
    decode: Callable[[Any], T],
) -> T:
    """Return a figure from the cache, or work it out and keep it."""
    try:
        key = f"{name}:{user_id}:{cache.get(_generation_key(user_id)) or 0}:{params}"
        saved = cache.get(key)
    except _CACHE_TROUBLE as error:
        log.warning("cache.unavailable", error=type(error).__name__)
        return compute()

    if saved is not None:
        try:
            return decode(saved)
        except _BAD_ENTRY:
            log.warning("cache.bad_entry", name=name)

    value = compute()
    try:
        cache.set(key, encode(value))
    except _CACHE_TROUBLE as error:
        log.warning("cache.unavailable", error=type(error).__name__)
    return value


def forget(user_id: int) -> None:
    """Make every figure saved for this account unreachable."""
    backend: BaseCache = cache.cache
    try:
        backend.inc(_generation_key(user_id))
    except _CACHE_TROUBLE as error:
        log.warning("cache.unavailable", error=type(error).__name__)


def _generation_key(user_id: int) -> str:
    return f"generation:{user_id}"


@event.listens_for(Session, "before_flush")
def _note_changes(session: Session, _context: UOWTransaction, _instances: object) -> None:
    for record in chain(session.new, session.dirty, session.deleted):
        if isinstance(record, _WATCHED):
            session.info.setdefault(_CHANGED_ACCOUNTS, set()).add(record.user_id)


@event.listens_for(Session, "after_commit")
def _forget_changed(session: Session) -> None:
    for user_id in session.info.pop(_CHANGED_ACCOUNTS, ()):
        forget(user_id)


@event.listens_for(Session, "after_rollback")
def _ignore_undone(session: Session) -> None:
    session.info.pop(_CHANGED_ACCOUNTS, None)
