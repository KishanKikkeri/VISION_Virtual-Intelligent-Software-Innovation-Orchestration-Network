"""
services/integration/plugin_sdk/plugin_manifest.py
=================================
M4.7 §1 Plugin Manifest — parses already-read manifest text (JSON or
TOML) into a `plugin_models.PluginManifest`. No filesystem access here
(that's `plugin_loader.py`'s job — same "pure algorithm over
already-fetched plain data" split every M4.x package uses); this
module only turns text/dict into a validated shape or raises a
`ManifestError` with a message good enough to show a plugin author
directly.

Recognized manifest filenames (used by `plugin_loader.py` to find the
manifest inside a plugin source): `manifest.json`, `plugin.json`,
`manifest.toml`.
"""
from __future__ import annotations

import json
import tomllib
from typing import Any, Dict

from pydantic import ValidationError

from services.integration.plugin_sdk.plugin_models import PluginManifest

MANIFEST_FILENAMES = ("manifest.json", "plugin.json", "manifest.toml")

_REQUIRED_FIELDS = ("id", "name", "version", "author", "entrypoint")


class ManifestError(Exception):
    """Raised for a manifest that cannot be parsed at all (malformed
    JSON/TOML, or missing a field `PluginManifest` requires). Distinct
    from a `ValidationIssue` (`plugin_validator.py`'s job) — a
    `ManifestError` means "this isn't a manifest," a `ValidationIssue`
    means "this is a manifest, but it has a problem.\""""


def parse_manifest(data: Dict[str, Any]) -> PluginManifest:
    """Turns an already-parsed dict into a `PluginManifest`, raising
    `ManifestError` (not a raw Pydantic `ValidationError`) with a
    plugin-author-readable message on failure."""
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise ManifestError(f"manifest is missing required field(s): {', '.join(missing)}")
    try:
        return PluginManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(f"manifest failed validation: {e}") from e


def load_manifest_json(content: str) -> PluginManifest:
    """Brief §1 entry point for a `manifest.json`/`plugin.json` file's
    text content."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError("manifest JSON must be an object at the top level")
    return parse_manifest(data)


def load_manifest_toml(content: str) -> PluginManifest:
    """Brief §1 entry point for a `manifest.toml` file's text content."""
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"manifest is not valid TOML: {e}") from e
    return parse_manifest(data)


def load_manifest(content: str, filename: str) -> PluginManifest:
    """Dispatches to `load_manifest_json`/`load_manifest_toml` by the
    manifest file's extension — the one function `plugin_loader.py`
    needs to call once it has located a manifest file's path/content."""
    if filename.endswith(".toml"):
        return load_manifest_toml(content)
    if filename.endswith(".json"):
        return load_manifest_json(content)
    raise ManifestError(f"unrecognized manifest filename {filename!r}; expected one of {MANIFEST_FILENAMES}")
