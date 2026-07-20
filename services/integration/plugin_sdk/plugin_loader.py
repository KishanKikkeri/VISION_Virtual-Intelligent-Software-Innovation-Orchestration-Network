"""
services/integration/plugin_sdk/plugin_loader.py
=================================
M4.7 §2 Plugin Loader — the one module in this package that actually
touches the filesystem/import machinery (every other module takes
already-parsed manifests/already-imported modules — same "pure
algorithm vs. I/O" split every M4.x package draws). Discovers plugin
sources under a `plugins/` directory and dynamically imports each
one's entrypoint module. **Never requires modifying core code**: a
plugin is discovered purely by its own manifest + entrypoint file
sitting under the configured plugins directory; nothing here edits
any existing platform file or registers anything at Python
import-time beyond the plugin's own module.

Three source types (brief §2), told apart by shape:

  - **python_package** — a directory directly under the plugins root
    containing a manifest file (see `plugin_manifest.MANIFEST_FILENAMES`)
    and the entrypoint `.py` file/package it names.
  - **zip_bundle** — a `.zip` file directly under the plugins root
    whose root contains a manifest file — loaded by extracting to a
    private temp directory (never executed in place inside the zip;
    Python can't import from inside an arbitrary zip layout reliably
    without `sys.path` surgery, so this module trades that for a
    clean, disposable extraction directory per load).
  - **editable** — a directory explicitly passed via `editable_paths`
    (not merely discovered under the plugins root) — the same
    directory a plugin author is actively developing in, so it's
    loaded in place, not copied.

**Trust boundary, stated plainly:** loading a plugin executes its
entrypoint module's top-level code. This is inherent to "third parties
extend the platform via dynamically loadable code" (brief's Goal) —
this module does not sandbox that execution (no subprocess isolation,
no restricted builtins). `plugin_validator.py`'s permission
declarations are a *manifest-level contract* a caller can choose to
enforce (e.g. via `plugin_runtime.context_for_plugin`, which only
populates context capabilities a plugin's manifest actually declared),
not a runtime sandbox — see `docs/M4.7_Plugin_SDK_Handover.md` §3 for
what a production deployment should add here (subprocess/gVisor/etc.
isolation) before loading untrusted third-party code.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import zipfile
from dataclasses import dataclass
from typing import List, Optional

from services.integration.plugin_sdk.plugin_manifest import MANIFEST_FILENAMES, ManifestError, load_manifest
from services.integration.plugin_sdk.plugin_models import PluginManifest, PluginSourceType


class PluginLoadError(Exception):
    """Raised when a plugin source's manifest can't be found/parsed,
    or its entrypoint module can't be imported."""


@dataclass
class DiscoveredPlugin:
    """One plugin source found under a plugins root (or passed as an
    editable path) — manifest already parsed, module *not* yet
    imported (see `import_entrypoint` for that, kept as a separate
    step so a caller can validate every discovered manifest — brief's
    "no duplicate IDs"/dependency-graph checks — before importing
    anything)."""
    manifest: PluginManifest
    source_type: PluginSourceType
    source_path: str  # directory (python_package/editable) or .zip file (zip_bundle)


class UnsafeZipMemberError(PluginLoadError):
    """Raised when a plugin zip bundle contains a member whose path
    would resolve outside the extraction directory (path traversal /
    "zip-slip", CWE-22). `zipfile.ZipFile.extractall` performs no such
    validation on its own — unlike `tarfile.TarFile.extractall`,
    `zipfile` has no `filter=` parameter (verified against the stdlib
    signature; the two modules' safety stories diverged in PEP 706,
    which only covers `tarfile`) — so this module validates every
    member itself before extracting any of them."""


def _safe_extractall(zf: zipfile.ZipFile, target_dir: str) -> None:
    """Validates every member's resolved path stays within
    `target_dir` before extracting anything, rejecting the whole
    bundle (not just the offending member) on the first violation —
    a plugin zip with even one traversal entry is untrustworthy as a
    whole, not just for that one file."""
    target_root = os.path.realpath(target_dir)
    for member in zf.namelist():
        resolved = os.path.realpath(os.path.join(target_root, member))
        if resolved != target_root and not resolved.startswith(target_root + os.sep):
            raise UnsafeZipMemberError(
                f"plugin zip bundle contains an unsafe member path {member!r} that would extract outside "
                f"the target directory — refusing to extract any part of this bundle")
    zf.extractall(target_root)


def _find_manifest_file(directory: str) -> Optional[str]:
    for filename in MANIFEST_FILENAMES:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_manifest_from_directory(directory: str) -> PluginManifest:
    manifest_path = _find_manifest_file(directory)
    if manifest_path is None:
        raise PluginLoadError(f"no manifest file found in {directory!r} (expected one of {MANIFEST_FILENAMES})")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        content = fh.read()
    try:
        return load_manifest(content, os.path.basename(manifest_path))
    except ManifestError as e:
        raise PluginLoadError(f"invalid manifest in {directory!r}: {e}") from e


def _load_manifest_from_zip(zip_path: str) -> PluginManifest:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            manifest_name = next((f for f in MANIFEST_FILENAMES if f in names), None)
            if manifest_name is None:
                raise PluginLoadError(f"no manifest file found at the root of {zip_path!r}")
            content = zf.read(manifest_name).decode("utf-8")
    except zipfile.BadZipFile as e:
        raise PluginLoadError(f"{zip_path!r} is not a valid zip file: {e}") from e
    try:
        return load_manifest(content, manifest_name)
    except ManifestError as e:
        raise PluginLoadError(f"invalid manifest in {zip_path!r}: {e}") from e


def discover_plugins(plugins_dir: str, editable_paths: Optional[List[str]] = None) -> List[DiscoveredPlugin]:
    """Brief §2 entry point. Scans `plugins_dir` for `python_package`
    (subdirectories with a manifest) and `zip_bundle` (`.zip` files
    with a manifest at their root) sources; `editable_paths` (if any)
    are loaded as `editable` regardless of where they live on disk.
    A source whose manifest can't be found/parsed is skipped with a
    logged reason rather than aborting discovery of every other
    plugin — one broken plugin source must not prevent discovering
    the rest (same "one failure doesn't crash the whole platform"
    principle the brief states for hook execution, applied here to
    discovery)."""
    discovered: List[DiscoveredPlugin] = []
    skipped: List[str] = []

    if os.path.isdir(plugins_dir):
        for entry in sorted(os.listdir(plugins_dir)):
            full_path = os.path.join(plugins_dir, entry)
            if os.path.isdir(full_path):
                try:
                    manifest = _load_manifest_from_directory(full_path)
                    discovered.append(DiscoveredPlugin(manifest, PluginSourceType.PYTHON_PACKAGE, full_path))
                except PluginLoadError as e:
                    skipped.append(f"{full_path}: {e}")
            elif entry.endswith(".zip"):
                try:
                    manifest = _load_manifest_from_zip(full_path)
                    discovered.append(DiscoveredPlugin(manifest, PluginSourceType.ZIP_BUNDLE, full_path))
                except PluginLoadError as e:
                    skipped.append(f"{full_path}: {e}")

    for editable_path in (editable_paths or []):
        try:
            manifest = _load_manifest_from_directory(editable_path)
            discovered.append(DiscoveredPlugin(manifest, PluginSourceType.EDITABLE, editable_path))
        except PluginLoadError as e:
            skipped.append(f"{editable_path}: {e}")

    discovered.sort(key=lambda d: d.manifest.id)
    return discovered


def _import_module_from_file(module_name: str, file_path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"could not build an import spec for {file_path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — surfaced as PluginLoadError, never left to propagate raw
        sys.modules.pop(module_name, None)
        raise PluginLoadError(f"entrypoint {file_path!r} raised on import: {e}") from e
    return module


def _entrypoint_file(source_dir: str, entrypoint: str) -> str:
    """Resolves a manifest's `entrypoint` (e.g. `\"plugin.py\"` or
    `\"plugin\"`) to an actual `.py` file under `source_dir`."""
    candidate = os.path.join(source_dir, entrypoint)
    if os.path.isfile(candidate):
        return candidate
    if os.path.isfile(candidate + ".py"):
        return candidate + ".py"
    init_candidate = os.path.join(candidate, "__init__.py")
    if os.path.isfile(init_candidate):
        return init_candidate
    raise PluginLoadError(f"entrypoint {entrypoint!r} not found under {source_dir!r}")


def import_entrypoint(discovered: DiscoveredPlugin, _extracted_dirs: Optional[dict] = None) -> types.ModuleType:
    """Brief §2 "Dynamic import only." Imports `discovered`'s
    entrypoint module and returns it — `plugin_runtime.py` then finds
    hook functions on this module by name (see that module's
    docstring). Each plugin gets its own unique `sys.modules` key
    (`aasc_plugin_<id>`) so two plugins can each ship a same-named
    `plugin.py` without colliding.

    `zip_bundle` sources are extracted to a fresh temp directory first
    (never imported from inside the zip); `_extracted_dirs`, if given,
    records `{plugin_id: temp_dir}` so a caller can clean those up
    later via `cleanup_extracted`."""
    module_name = f"aasc_plugin_{discovered.manifest.id}"

    if discovered.source_type == PluginSourceType.ZIP_BUNDLE:
        temp_dir = tempfile.mkdtemp(prefix=f"aasc_plugin_{discovered.manifest.id}_")
        with zipfile.ZipFile(discovered.source_path) as zf:
            _safe_extractall(zf, temp_dir)
        if _extracted_dirs is not None:
            _extracted_dirs[discovered.manifest.id] = temp_dir
        entrypoint_path = _entrypoint_file(temp_dir, discovered.manifest.entrypoint)
    else:
        entrypoint_path = _entrypoint_file(discovered.source_path, discovered.manifest.entrypoint)

    return _import_module_from_file(module_name, entrypoint_path)


def cleanup_extracted(extracted_dirs: dict, plugin_id: str) -> None:
    """Removes the temp extraction directory `import_entrypoint`
    created for a `zip_bundle` plugin — a caller uninstalling or
    reloading a zip-sourced plugin should call this to avoid leaking
    temp directories. A no-op if `plugin_id` has no recorded extraction
    (e.g. it was never a zip bundle)."""
    temp_dir = extracted_dirs.pop(plugin_id, None)
    if temp_dir and os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


def validate_entrypoint_importable(discovered: DiscoveredPlugin) -> Optional[str]:
    """Brief §8 "imports" validation check — attempts the import and
    returns `None` on success or an error message on failure, without
    raising; a caller (e.g. `plugin_validator`-adjacent install flow)
    can fold this into a `ValidationIssue` alongside the purely
    static checks in `plugin_validator.py`. Leaves the imported module
    in `sys.modules` on success (a subsequent real load reuses it via
    `import_entrypoint`'s own fresh `exec_module` call, which
    overwrites the same key) — this function's job is only to answer
    "can it import," not to manage the module's lifecycle."""
    try:
        import_entrypoint(discovered)
    except PluginLoadError as e:
        return str(e)
    return None
