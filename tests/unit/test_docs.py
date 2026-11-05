"""The documents stay true: everything they should mention is there, and their links work."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import create_app
from app.config import Settings
from app.extensions import db
from app.jobs import cleanup
from app.services.auth import RECENT_SIGN_INS
from tests.conftest import make_settings

ROOT = Path(__file__).parents[2]
DOCS = sorted([ROOT / "README.md", *(ROOT / "docs").rglob("*.md")])
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.fixture(scope="module")
def flask_app():
    return create_app(make_settings(database_url="postgresql://unused@localhost/unused"))


def test_every_endpoint_is_documented(flask_app):
    documented = (ROOT / "docs/endpoints.md").read_text()
    missing = []
    for rule in flask_app.url_map.iter_rules():
        methods = " or ".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        row = re.search(rf"^\| ([A-Z, ]+) \| `{re.escape(rule.rule)}` \|", documented, re.MULTILINE)
        if row is None:
            missing.append(f"{methods} {rule.rule}")
        elif set(row.group(1).split(", ")) != rule.methods - {"HEAD", "OPTIONS"}:
            missing.append(f"{methods} {rule.rule}, which says {row.group(1)}")
    assert not missing


def test_nothing_documented_has_gone(flask_app):
    documented = re.findall(
        r"^\| [A-Z, ]+ \| `([^`]+)` \|", (ROOT / "docs/endpoints.md").read_text(), re.MULTILINE
    )
    assert set(documented) == {rule.rule for rule in flask_app.url_map.iter_rules()}


def test_every_table_and_column_is_documented():
    schema = (ROOT / "docs/schema.md").read_text()
    sections = re.split(r"^### ", schema, flags=re.MULTILINE)[1:]
    by_table = {section.split("\n", 1)[0].strip(): section for section in sections}
    missing = []
    for table in db.metadata.sorted_tables:
        section = by_table.get(table.name)
        if section is None:
            missing.append(table.name)
            continue
        missing += [
            f"{table.name}.{column.name}"
            for column in table.columns
            if f"`{column.name}`" not in section
        ]
    assert not missing


def test_nothing_documented_has_been_removed_from_the_schema():
    documented = set(
        re.findall(r"^### (\w+)$", (ROOT / "docs/schema.md").read_text(), re.MULTILINE)
    )
    assert documented == {table.name for table in db.metadata.sorted_tables}


@pytest.mark.parametrize("document", DOCS, ids=lambda path: str(path.relative_to(ROOT)))
def test_links_between_documents_work(document):
    for target in LINK.findall(document.read_text()):
        if target.startswith(("http://", "https://", "/")):
            continue
        path, _, anchor = target.partition("#")
        linked = (document.parent / path).resolve() if path else document
        assert linked.exists(), f"{document.name} links to {target}"
        if anchor:
            headings = re.findall(r"^#+ (.+)$", linked.read_text(), re.MULTILINE)
            slugs = {re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings}
            assert anchor in slugs, f"{document.name} links to {target}"


def test_make_targets_mentioned_in_the_readme_exist():
    makefile = (ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z-]+):", makefile, re.MULTILINE))
    mentioned = set(re.findall(r"(?:^|`)make ([a-z-]+)", (ROOT / "README.md").read_text(), re.MULTILINE))
    assert mentioned <= targets


def test_every_setting_is_in_the_example_file():
    example = (ROOT / ".env.example").read_text()
    names = {name.upper() for name in Settings.model_fields}
    assert not {name for name in names if not re.search(rf"^{name}=", example, re.MULTILINE)}


def test_every_setting_the_stack_uses_is_in_its_example_file():
    compose = (ROOT / "deploy/compose.prod.yml").read_text()
    example = (ROOT / "deploy/stack.env.example").read_text()
    used = set(re.findall(r"\$\{([A-Z_]+)", compose))
    # IMAGE_TAG is set by pi-deploy.sh for each release, rather than kept in the file.
    assert not {
        name for name in used - {"IMAGE_TAG"} if not re.search(rf"^{name}=", example, re.MULTILINE)
    }


def test_the_retention_periods_people_are_told_are_the_real_ones():
    privacy = (ROOT / "app/templates/pages/privacy.html").read_text()
    guide = (ROOT / "docs/user-guide.md").read_text()
    readme = (ROOT / "README.md").read_text()
    for text in (privacy, guide):
        assert f"removed after {cleanup.DELETED_SALES_DAYS} days" in text
        assert f"older than {cleanup.SIGN_IN_DAYS} days" in text
        assert f"latest {RECENT_SIGN_INS}" in text
        assert f"removed after {cleanup.READ_NOTIFICATION_DAYS} days" in text
    assert f"deleted over {cleanup.DELETED_SALES_DAYS} days ago" in readme
    assert f"older than {cleanup.SIGN_IN_DAYS} days" in readme
    assert f"older than {cleanup.READ_NOTIFICATION_DAYS} days" in readme


def test_the_workflows_pin_every_action_to_a_commit():
    """A tag can be moved to different code. A commit can't."""
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"uses: (\S+)", workflow.read_text()):
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), f"{workflow.name}: {action}"


def test_the_image_never_holds_anything_private():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    for name in (".env", ".env.*", ".git", "tests", "node_modules"):
        assert name in ignored
