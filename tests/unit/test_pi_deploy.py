"""The deploy script, with a stand-in for Docker that records what it was asked to do."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "deploy/pi-deploy.sh"
OLD = "a" * 40
NEW = "b" * 40
NEWER = "c" * 40

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "IMAGE_TAG=${IMAGE_TAG:-} BACKUP_PATH=${BACKUP_PATH:-} $*" >> "$STATE/calls"
if [[ -f $STATE/fail-on ]]; then
    while read -r rule; do
        [[ -z $rule ]] && continue
        if [[ "IMAGE_TAG=${IMAGE_TAG:-} $*" == *"$rule"* || "$*" == *"$rule"* ]]; then
            exit 1
        fi
    done < "$STATE/fail-on"
fi
case "$1" in
    image)
        if [[ $2 == inspect ]]; then cat "$STATE/revision"; fi
        ;;
    run)
        # Taking a file out of an image: its contents say which image it came from.
        for arg in "$@"; do
            case "$arg" in *selltrack:*) image=$arg ;; esac
        done
        echo "${*: -1} from $image"
        ;;
esac
exit 0
"""


class Stack:
    def __init__(self, folder: Path, bin_dir: Path, state: Path) -> None:
        self.folder = folder
        self.bin_dir = bin_dir
        self.state = state

    def release_is(self, revision: str) -> None:
        (self.state / "revision").write_text(revision + "\n")

    def fails_on(self, *rules: str) -> None:
        (self.state / "fail-on").write_text("\n".join(rules) + "\n")

    def clear_failures(self) -> None:
        (self.state / "fail-on").unlink(missing_ok=True)

    def deploy(self) -> subprocess.CompletedProcess[str]:
        calls = self.state / "calls"
        calls.unlink(missing_ok=True)
        return subprocess.run(  # noqa: S603 - our own script, in a test
            [str(SCRIPT), str(self.folder)],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
                "STATE": str(self.state),
            },
        )

    @property
    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def compose_calls(self) -> list[str]:
        """Each compose call as the tag it used and what it did, without the shared options."""
        found = []
        for line in self.calls:
            if " compose " in line:
                tag = line.split()[0].removeprefix("IMAGE_TAG=")
                found.append(f"{tag}: {line.split('compose.prod.yml ', 1)[1]}")
        return found

    def tag(self, name: str) -> str | None:
        path = self.folder / name
        return path.read_text().strip() if path.exists() else None


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    folder = tmp_path / "opt/prod"
    folder.mkdir(parents=True)
    (folder / ".env").write_text(
        "IMAGE_REPO=ghcr.io/example/selltrack\nIMAGE_CHANNEL=prod\nSECRET_KEY=x\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    made = Stack(folder, bin_dir, state)
    made.release_is(OLD)
    return made


def deployed(stack: Stack, revision: str = OLD) -> Stack:
    stack.release_is(revision)
    assert stack.deploy().returncode == 0
    return stack


def steps(tag: str) -> list[str]:
    wait = "--detach --wait --wait-timeout 180"
    return [
        f"{tag}: up {wait} db redis",
        f"{tag}: run --rm db-roles",
        f"{tag}: run --rm migrate",
        f"{tag}: run --rm db-roles",
        f"{tag}: up {wait} --remove-orphans",
        f"{tag}: run --rm smoke",
    ]


class TestDeploying:
    def test_first_deploy_does_everything_in_order(self, stack):
        result = stack.deploy()
        assert result.returncode == 0, result.stdout + result.stderr
        assert stack.compose_calls == steps(f"sha-{OLD}")
        assert stack.tag("deployed-tag") == f"sha-{OLD}"
        assert f"now running sha-{OLD}" in result.stdout

    def test_it_pulls_both_images_of_the_release(self, stack):
        stack.deploy()
        pulls = [call for call in stack.calls if " pull " in call]
        assert any(f"ghcr.io/example/selltrack:sha-{OLD}" in call for call in pulls)
        assert any(f"ghcr.io/example/selltrack-nginx:sha-{OLD}" in call for call in pulls)

    def test_the_compose_file_comes_from_the_image_being_deployed(self, stack):
        deployed(stack)
        compose = (stack.folder / "release/compose.prod.yml").read_text()
        assert compose == f"deploy/compose.prod.yml from ghcr.io/example/selltrack:sha-{OLD}\n"
        for name in ("db-roles.sql", "backup.sh", "pi-deploy.sh"):
            assert (stack.folder / "release" / name).exists()

    def test_compose_is_told_where_everything_is(self, stack):
        deployed(stack)
        compose = next(call for call in stack.calls if " compose " in call)
        assert "--project-name selltrack-prod" in compose
        assert f"--project-directory {stack.folder}/release" in compose
        assert f"--env-file {stack.folder}/.env" in compose
        assert f"--file {stack.folder}/release/compose.prod.yml" in compose

    def test_backups_default_to_a_folder_in_the_stack(self, stack):
        deployed(stack)
        assert (stack.folder / "backups").is_dir()
        assert f"BACKUP_PATH={stack.folder}/backups" in stack.calls[-2]

    def test_backups_can_go_somewhere_else(self, stack, tmp_path):
        elsewhere = tmp_path / "ssd/backups"
        with (stack.folder / ".env").open("a") as env:
            env.write(f"BACKUP_PATH={elsewhere}\n")
        deployed(stack)
        assert elsewhere.is_dir()
        assert f"BACKUP_PATH={elsewhere}" in stack.calls[-2]

    def test_nothing_happens_when_nothing_has_changed(self, stack):
        deployed(stack)
        result = stack.deploy()
        assert result.returncode == 0
        assert result.stdout == ""  # or the log would fill up, at one line every two minutes
        assert stack.compose_calls == []

    def test_a_new_release_replaces_the_old_one(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        result = stack.deploy()
        assert result.returncode == 0, result.stdout + result.stderr
        assert stack.compose_calls == steps(f"sha-{NEW}")
        assert stack.tag("deployed-tag") == f"sha-{NEW}"
        assert OLD in (stack.folder / "release.previous/compose.prod.yml").read_text()

    def test_old_images_are_tidied_up(self, stack):
        deployed(stack)
        assert any("image prune --all --force --filter until=336h" in c for c in stack.calls)

    def test_a_failed_tidy_up_doesnt_fail_the_deploy(self, stack):
        stack.fails_on("image prune")
        assert stack.deploy().returncode == 0
        assert stack.tag("deployed-tag") == f"sha-{OLD}"

    def test_says_when_this_script_is_out_of_date(self, stack):
        result = stack.deploy()
        assert "newer pi-deploy.sh" in result.stdout


class TestWhenItGoesWrong:
    def test_failed_smoke_check_goes_back(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("run --rm smoke")
        result = stack.deploy()
        assert result.returncode == 1
        assert stack.compose_calls == [
            *steps(f"sha-{NEW}"),
            f"sha-{OLD}: up --detach --wait --wait-timeout 180 --remove-orphans",
        ]
        assert stack.tag("deployed-tag") == f"sha-{OLD}"
        assert stack.tag("failed-tag") == f"sha-{NEW}"
        assert OLD in (stack.folder / "release/compose.prod.yml").read_text()
        assert f"back on sha-{OLD}" in result.stdout

    def test_containers_that_wont_start_go_back(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("--remove-orphans")
        result = stack.deploy()
        assert result.returncode == 1
        assert stack.tag("failed-tag") == f"sha-{NEW}"
        assert "run --rm smoke" not in " ".join(stack.compose_calls)

    def test_a_bad_release_isnt_tried_again(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("run --rm smoke")
        assert stack.deploy().returncode == 1
        result = stack.deploy()
        assert result.returncode == 0
        assert stack.compose_calls == []
        assert "failed earlier" in result.stdout

    def test_a_different_release_is_tried_and_clears_the_failure(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("run --rm smoke")
        stack.deploy()
        stack.clear_failures()
        stack.release_is(NEWER)
        assert stack.deploy().returncode == 0
        assert stack.tag("deployed-tag") == f"sha-{NEWER}"
        assert stack.tag("failed-tag") is None

    @pytest.mark.parametrize(
        "rule",
        ["up --detach --wait --wait-timeout 180 db redis", "run --rm db-roles", "run --rm migrate"],
    )
    def test_trouble_with_the_database_leaves_the_old_release_running(self, stack, rule):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on(rule)
        result = stack.deploy()
        assert result.returncode == 1
        assert not [call for call in stack.compose_calls if call.endswith("--remove-orphans")]
        assert stack.tag("deployed-tag") == f"sha-{OLD}"
        assert stack.tag("failed-tag") == f"sha-{NEW}"
        assert OLD in (stack.folder / "release/compose.prod.yml").read_text()
        assert "carries on" in result.stdout

    def test_first_deploy_failing_has_nothing_to_go_back_to(self, stack):
        stack.fails_on("run --rm smoke")
        result = stack.deploy()
        assert result.returncode == 1
        assert stack.tag("deployed-tag") is None
        assert stack.tag("failed-tag") == f"sha-{OLD}"
        assert "nothing earlier to go back to" in result.stdout

    def test_says_so_if_going_back_fails_too(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("--remove-orphans")
        result = stack.deploy()
        assert result.returncode == 1
        assert "didn't work" in result.stdout

    def test_cant_reach_the_registry(self, stack):
        deployed(stack)
        stack.release_is(NEW)
        stack.fails_on("pull --quiet ghcr.io/example/selltrack:prod")
        result = stack.deploy()
        assert result.returncode != 0
        assert stack.compose_calls == []
        assert stack.tag("deployed-tag") == f"sha-{OLD}"
        assert stack.tag("failed-tag") is None  # it can try again when the network's back

    @pytest.mark.parametrize("label", ["", "unknown", "abc123", "<no value>", OLD + "; rm -rf /"])
    def test_an_image_without_a_proper_revision_is_refused(self, stack, label):
        stack.release_is(label)
        result = stack.deploy()
        assert result.returncode == 1
        assert "no usable revision" in result.stdout
        assert stack.compose_calls == []

    @pytest.mark.parametrize("missing", ["IMAGE_REPO", "IMAGE_CHANNEL"])
    def test_settings_are_required(self, stack, missing):
        env = stack.folder / ".env"
        env.write_text(
            "".join(
                line + "\n" for line in env.read_text().splitlines() if not line.startswith(missing)
            )
        )
        result = stack.deploy()
        assert result.returncode == 1
        assert "must be set" in result.stdout

    def test_only_one_at_a_time(self, stack):
        with (stack.folder / ".deploy.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = stack.deploy()
        assert result.returncode == 0
        assert "another deploy" in result.stdout
        assert stack.calls == []

    def test_needs_to_be_told_which_stack(self):
        result = subprocess.run(  # noqa: S603 - our own script, in a test
            [str(SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode != 0
        assert "usage" in result.stderr
