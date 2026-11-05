"""Finds built CSS and JavaScript, whose filenames contain a hash of their contents."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import structlog
from flask import Flask, url_for

_MANIFEST = Path(__file__).parent / "static" / "dist" / "manifest.json"

log = structlog.get_logger("selltrack.assets")


@lru_cache(maxsize=1)
def _read_manifest(modified: float) -> dict[str, str]:
    """Read the manifest. Taking the file's modified time means a rebuild is picked up by itself."""
    manifest: dict[str, str] = json.loads(_MANIFEST.read_text())
    log.debug("assets.manifest_read", modified=modified)
    return manifest


def asset_url(name: str) -> str:
    """The address of a built file, like ``app.css``. Pages still render if nothing is built."""
    try:
        manifest = _read_manifest(_MANIFEST.stat().st_mtime)
    except FileNotFoundError:
        log.warning("assets.not_built", detail="Run `npm run build`. Pages will be unstyled.")
        manifest = {}
    return url_for("static", filename=f"dist/{manifest.get(name, name)}")


def init_app(app: Flask) -> None:
    app.jinja_env.globals["asset"] = asset_url
