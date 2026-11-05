"""The backup script's own logic, with stand-ins for pg_dump and pg_restore.

``tests/integration/test_db_roles.py`` runs it for real, and restores what it makes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "deploy/backup.sh"

FAKE_PG_DUMP = """#!/bin/sh
for arg in "$@"; do
    case "$arg" in --file=*) file="${arg#--file=}" ;; esac
done
if [ -n "${FAIL_DUMP:-}" ]; then echo partial > "$file"; exit 1; fi
echo "a dump" > "$file"
"""
FAKE_PG_RESTORE = """#!/bin/sh
if [ -n "${FAIL_READ:-}" ]; then exit 1; fi
exit 0
"""
# Stops the script at its first wait, after noting how long it would have waited.
FAKE_SLEEP = """#!/bin/sh
echo "$1" > "$SLEEP_RECORD"
kill -TERM "$PPID"
"""


def install(folder: Path, name: str, content: str) -> None:
    path = folder / name
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install(bin_dir, "pg_dump", FAKE_PG_DUMP)
    install(bin_dir, "pg_restore", FAKE_PG_RESTORE)
    install(bin_dir, "sleep", FAKE_SLEEP)
    backups = tmp_path / "backups"
    backups.mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BACKUP_DIR": str(backups),
        "SLEEP_RECORD": str(tmp_path / "slept"),
    }


def run(env: dict[str, str], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script, in a test
        ["sh", str(SCRIPT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**env, **extra},
    )


def files(env: dict[str, str]) -> list[Path]:
    return sorted(Path(env["BACKUP_DIR"]).iterdir())


def old_backup(env: dict[str, str], name: str, *, age: timedelta) -> Path:
    path = Path(env["BACKUP_DIR"]) / f"selltrack-{name}.dump"
    path.write_text("old")
    stamp = (datetime.now(UTC) - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class TestOnce:
    def test_makes_one_file_only_its_owner_can_read(self, env):
        result = run(env, "--once")
        assert result.returncode == 0, result.stdout + result.stderr
        [backup] = files(env)
        assert backup.name.startswith("selltrack-")
        assert backup.name.endswith(".dump")
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert "saved" in result.stdout

    def test_keeps_only_the_newest(self, env):
        for number in range(20):
            old_backup(env, f"2026010{number:02d}", age=timedelta(days=number + 1))
        result = run(env, "--once", BACKUP_KEEP="14")
        assert result.returncode == 0
        kept = files(env)
        assert len(kept) == 14
        assert not (Path(env["BACKUP_DIR"]) / "selltrack-202601019.dump").exists()
        assert "removed" in result.stdout

    def test_leaves_other_files_alone(self, env):
        other = Path(env["BACKUP_DIR"]) / "notes.txt"
        other.write_text("mine")
        for number in range(3):
            old_backup(env, f"old{number}", age=timedelta(days=number + 1))
        run(env, "--once", BACKUP_KEEP="1")
        assert other.exists()

    def test_a_failed_dump_leaves_nothing_behind(self, env):
        result = run(env, "--once", FAIL_DUMP="1")
        assert result.returncode == 1
        assert files(env) == []
        assert "pg_dump failed" in result.stdout

    def test_a_dump_that_cant_be_read_isnt_kept(self, env):
        result = run(env, "--once", FAIL_READ="1")
        assert result.returncode == 1
        assert files(env) == []
        assert "can't be read" in result.stdout

    def test_a_failure_keeps_the_old_backups(self, env):
        old = old_backup(env, "old", age=timedelta(days=1))
        run(env, "--once", FAIL_DUMP="1")
        assert old.exists()


class TestRunning:
    @staticmethod
    def hour_ago() -> str:
        return str((datetime.now(UTC) - timedelta(hours=1)).hour)

    def slept(self, env: dict[str, str]) -> float:
        return float(Path(env["SLEEP_RECORD"]).read_text())

    def test_takes_one_straight_away_if_there_are_none(self, env):
        result = run(env, BACKUP_HOUR_UTC=self.hour_ago())
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(files(env)) == 1
        # Then waits for the next day's, about 23 hours away.
        assert 22 * 3600 < self.slept(env) <= 24 * 3600

    def test_catches_up_if_it_missed_one(self, env):
        old_backup(env, "old", age=timedelta(days=2))
        run(env, BACKUP_HOUR_UTC=self.hour_ago())
        assert len(files(env)) == 2

    def test_waits_if_it_already_has_tonights(self, env):
        (Path(env["BACKUP_DIR"]) / "selltrack-recent.dump").write_text("new")
        run(env, BACKUP_HOUR_UTC=self.hour_ago())
        assert len(files(env)) == 1
        assert 22 * 3600 < self.slept(env) <= 24 * 3600

    def test_tries_again_later_if_it_fails(self, env):
        result = run(env, BACKUP_HOUR_UTC=self.hour_ago(), FAIL_DUMP="1")
        assert result.returncode == 0  # the container carries on
        assert files(env) == []
        assert self.slept(env) == 900

    def test_stops_cleanly_when_told(self, env):
        started = time.monotonic()
        result = run(env, BACKUP_HOUR_UTC=self.hour_ago())
        assert result.returncode == 0
        assert "stopping" in result.stdout
        assert time.monotonic() - started < 10
