"""How the server is doing, for health checks and the admin's system page."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis
import structlog
from flask import current_app
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import AuditLog

log = structlog.get_logger("selltrack.system")

_REDIS_TIMEOUT_SECONDS = 1
#: Backups run nightly, so this allows a late one but not a missed one (plan section 8).
BACKUP_MAX_AGE = timedelta(hours=26)
CLEANUP_MAX_AGE = timedelta(hours=26)
DISK_WARNING_PERCENT = 85
_PERCENT = 100


@dataclass(frozen=True, slots=True)
class Disk:
    path: str
    total_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> int:
        if not self.total_bytes:
            return 0
        return round((self.total_bytes - self.free_bytes) * _PERCENT / self.total_bytes)

    @property
    def is_low(self) -> bool:
        return self.used_percent >= DISK_WARNING_PERCENT


@dataclass(frozen=True, slots=True)
class Backup:
    """The newest finished file in the backup folder. Nothing's checked about what's inside it."""

    configured: bool
    last_at: datetime | None = None
    size_bytes: int | None = None
    is_late: bool = False


@dataclass(frozen=True, slots=True)
class Health:
    database_ok: bool
    database_bytes: int | None
    redis: str
    disk: Disk | None
    backup: Backup
    last_cleanup: datetime | None
    cleanup_is_late: bool

    @property
    def needs_attention(self) -> bool:
        return (
            not self.database_ok
            or self.redis == "degraded"
            or self.disk is None
            or self.disk.is_low
            or self.backup.is_late
            or self.cleanup_is_late
        )


def database_ok() -> bool:
    try:
        db.session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as error:
        log.error("health.database_unavailable", error=type(error).__name__)
        db.session.rollback()
        return False
    return True


def redis_status() -> str:
    url = current_app.config["REDIS_URL"]
    if not url:
        return "not_configured"
    try:
        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_TIMEOUT_SECONDS,
        )
        try:
            client.ping()
        finally:
            client.close()
    except (redis.RedisError, OSError) as error:
        log.warning("health.redis_unavailable", error=type(error).__name__)
        return "degraded"
    return "ok"


def disk_usage(path: Path) -> Disk | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError as error:
        log.warning("health.disk_unavailable", error=type(error).__name__)
        return None
    return Disk(path=str(path), total_bytes=usage.total, free_bytes=usage.free)


def last_backup(folder: Path | None, *, now: datetime | None = None) -> Backup:
    if folder is None:
        return Backup(configured=False)
    now = now or datetime.now(UTC)
    newest: os.stat_result | None = None
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                # backup.sh writes to a dotfile, and only renames it once it has been checked.
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    details = entry.stat(follow_symlinks=False)
                    if newest is None or details.st_mtime > newest.st_mtime:
                        newest = details
    except OSError as error:
        log.warning("health.backup_folder_unavailable", error=type(error).__name__)
    if newest is None:
        return Backup(configured=True, is_late=True)
    when = datetime.fromtimestamp(newest.st_mtime, UTC)
    return Backup(
        configured=True,
        last_at=when,
        size_bytes=newest.st_size,
        is_late=now - when > BACKUP_MAX_AGE,
    )


def last_cleanup() -> datetime | None:
    return db.session.scalar(
        select(func.max(AuditLog.created_at)).where(AuditLog.action == "system.cleanup")
    )


def health(*, now: datetime | None = None) -> Health:
    now = now or datetime.now(UTC)
    ok = database_ok()
    folder: Path | None = current_app.config["BACKUP_DIR"]
    cleanup = last_cleanup() if ok else None
    return Health(
        database_ok=ok,
        database_bytes=(
            db.session.scalar(select(func.pg_database_size(func.current_database())))
            if ok
            else None
        ),
        redis=redis_status(),
        disk=disk_usage(folder or Path("/")),
        backup=last_backup(folder, now=now),
        last_cleanup=cleanup,
        cleanup_is_late=cleanup is None or now - cleanup > CLEANUP_MAX_AGE,
    )
