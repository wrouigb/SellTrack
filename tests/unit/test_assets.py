"""Finding built CSS and JavaScript."""

from __future__ import annotations

import json
import os

import pytest
from flask import Flask

from app import assets


@pytest.fixture
def flask_app():
    app = Flask("test", static_folder="static")
    with app.test_request_context("/"):
        yield app


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    monkeypatch.setattr(assets, "_MANIFEST", path)
    assets._read_manifest.cache_clear()
    yield path
    assets._read_manifest.cache_clear()


def test_uses_the_hashed_name(flask_app, manifest):
    manifest.write_text(json.dumps({"app.css": "styles-ABC123.css"}))
    assert assets.asset_url("app.css") == "/static/dist/styles-ABC123.css"


def test_a_rebuild_is_picked_up_without_a_restart(flask_app, manifest):
    manifest.write_text(json.dumps({"app.css": "old.css"}))
    assert assets.asset_url("app.css").endswith("old.css")
    manifest.write_text(json.dumps({"app.css": "new.css"}))
    # Make sure the file looks modified, however quickly this runs.
    stat = manifest.stat()
    os.utime(manifest, (stat.st_atime, stat.st_mtime + 5))
    assert assets.asset_url("app.css").endswith("new.css")


def test_pages_still_render_when_nothing_is_built(flask_app, manifest):
    assert assets.asset_url("app.css") == "/static/dist/app.css"
