"""
services/integration/release_validation/compatibility_matrix.py
=================================
M4.10 §1 Release Validation — component compatibility matrix (Python,
Docker, Redis, Postgres, NATS versions against the ranges this platform
declares support for). Purely a version-string comparator; it never
opens a live connection itself — `detected_version` is always supplied
by the caller (the installer/CLI probes actually establish connections,
see §4 `scripts/install/verify.py`), so this module stays synchronous
and dependency-free like M4.9's `deployment_validator.py`.
"""
from __future__ import annotations

import re
from typing import Dict, Optional

from services.integration.release_validation.release_validation_models import (
    CompatibilityEntry, CompatibilityMatrix,
)

# Declared support ranges for this platform. Kept here (not read from a
# config file) because these are release-engineering constants, not
# environment-specific configuration — same reasoning M4.9's
# `deployment_profiles.py` used for its base profile dict.
DEFAULT_REQUIRED_RANGES: Dict[str, str] = {
    "python": ">=3.11,<4.0",
    "postgres": ">=14,<17",
    "redis": ">=6.2,<8.0",
    "nats": ">=2.9,<3.0",
    "docker": ">=24.0,<28.0",
}

_RANGE_RE = re.compile(r"(>=|<=|>|<|==)\s*([0-9]+(?:\.[0-9]+)*)")


def _parse_version(version: str) -> tuple:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _satisfies(detected: str, required_range: str) -> bool:
    detected_t = _parse_version(detected)
    for op, bound in _RANGE_RE.findall(required_range):
        bound_t = _parse_version(bound)
        # pad to equal length for tuple comparison
        length = max(len(detected_t), len(bound_t))
        d = detected_t + (0,) * (length - len(detected_t))
        b = bound_t + (0,) * (length - len(bound_t))
        if op == ">=" and not d >= b:
            return False
        if op == ">" and not d > b:
            return False
        if op == "<=" and not d <= b:
            return False
        if op == "<" and not d < b:
            return False
        if op == "==" and not d == b:
            return False
    return True


def check_component(component: str, detected_version: Optional[str],
                     required_range: Optional[str] = None) -> CompatibilityEntry:
    required = required_range or DEFAULT_REQUIRED_RANGES.get(component, "")
    if detected_version is None:
        return CompatibilityEntry(component=component, required_range=required, detected_version=None,
                                   compatible=None, note=f"{component} not detected in this environment")
    if not required:
        return CompatibilityEntry(component=component, required_range="", detected_version=detected_version,
                                   compatible=None, note=f"no declared compatibility range for {component}")
    ok = _satisfies(detected_version, required)
    note = "compatible" if ok else f"{detected_version} does not satisfy {required}"
    return CompatibilityEntry(component=component, required_range=required, detected_version=detected_version,
                               compatible=ok, note=note)


def build_compatibility_matrix(detected_versions: Optional[Dict[str, str]] = None,
                                required_ranges: Optional[Dict[str, str]] = None) -> CompatibilityMatrix:
    """`detected_versions` is a plain `{component: version_string}` map —
    a caller-supplied probe result, exactly the same "no assumed backend"
    convention M4.9's `backup_manager.create_backup(fetchers=...)`
    established. Components with no detected version still appear in the
    matrix as `compatible=None` rather than being silently omitted."""
    detected_versions = detected_versions or {}
    ranges = {**DEFAULT_REQUIRED_RANGES, **(required_ranges or {})}
    entries = [check_component(name, detected_versions.get(name), req) for name, req in ranges.items()]
    # also surface any detected component outside the declared set
    for name, version in detected_versions.items():
        if name not in ranges:
            entries.append(check_component(name, version, None))
    return CompatibilityMatrix(entries=entries)
