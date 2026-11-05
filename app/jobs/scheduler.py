"""A small process that runs the nightly cleanup. ``flask jobs scheduler`` starts it.

There's no queue because there's only one job. If the process was down when the job was due, it
runs as soon as the process is back.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.jobs.cleanup import CleanupResult, run_cleanup
from app.services.system import last_cleanup

log = structlog.get_logger("selltrack.scheduler")

#: About 3 or 4 in the morning in Melbourne, when nobody should be using it.
CLEANUP_HOUR_UTC = 17
#: How often to look again, however long until the next run, so a clock change can't lose it.
_LONGEST_WAIT_SECONDS = 900.0
_RETRY_SECONDS = 900.0


def seconds_until_due(last_run: datetime | None, now: datetime, *, hour: int) -> float:
    """How long to wait, or 0 if the most recent nightly slot hasn't been run yet."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    if last_run is None or last_run < slot:
        return 0.0
    return (slot + timedelta(days=1) - now).total_seconds()


def run_forever(
    stop: threading.Event,
    *,
    job: Callable[[], CleanupResult | None] = run_cleanup,
    hour: int = CLEANUP_HOUR_UTC,
) -> None:
    """Keep the job running on time until ``stop`` is set. Database trouble is retried."""
    log.info("scheduler.started", hour_utc=hour)
    while not stop.is_set():
        try:
            stop.wait(_tick(job, hour))
        except SQLAlchemyError as error:
            log.error("scheduler.database_trouble", error=type(error).__name__)
            db.session.rollback()
            stop.wait(_RETRY_SECONDS)
        finally:
            # Nothing should sit holding a connection for hours between runs.
            db.session.remove()
    log.info("scheduler.stopped")


def _tick(job: Callable[[], CleanupResult | None], hour: int) -> float:
    """Run the job if it's due. Returns how long to wait before looking again."""
    wait = seconds_until_due(last_cleanup(), datetime.now(UTC), hour=hour)
    if wait > 0:
        return min(wait, _LONGEST_WAIT_SECONDS)
    result = job()
    if result is None:
        # Another process has the lock, and until it finishes this one still looks overdue.
        log.info("scheduler.already_running")
        return _RETRY_SECONDS
    log.info(
        "scheduler.cleanup_done",
        sales=result.sales,
        sign_ins=result.sign_ins,
        notifications=result.notifications,
    )
    return 0.0
