"""
services/integration/release_validation/release_packaging.py
=================================
M4.10 §8 Release Packaging — builds `release/release_manifest.json`
by checking for the presence and checksum of `release/CHANGELOG.md`,
`RELEASE_NOTES.md`, `LICENSE`, `NOTICE`, `VERSION`, plus
`docs/generated/` and `benchmarks/benchmark.json`. Never generates
CHANGELOG/RELEASE_NOTES prose itself — those are authored content (see
`release/CHANGELOG.md` / `release/RELEASE_NOTES.md` in this same
milestone), this module only inventories and checksums them, the same
"inventory, don't author" split `release_manager.build_artifact_
inventory` uses in M4.9.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import List, Optional

from services.integration.release_validation.release_validation_models import ReleaseManifest, ReleaseManifestEntry

_EXPECTED_FILES = [
    ("release/CHANGELOG.md", "changelog"),
    ("release/RELEASE_NOTES.md", "release_notes"),
    ("release/LICENSE", "license"),
    ("release/NOTICE", "notice"),
    ("release/VERSION", "version"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_release_manifest(version: str, root: str = ".") -> ReleaseManifest:
    entries: List[ReleaseManifestEntry] = []
    for rel_path, kind in _EXPECTED_FILES:
        path = os.path.join(root, rel_path)
        entries.append(ReleaseManifestEntry(path=rel_path, kind=kind, present=os.path.isfile(path),
                                             checksum=_checksum(path)))

    docs_dir = os.path.join(root, "docs", "generated")
    docs_present = os.path.isdir(docs_dir) and any(f.endswith(".md") for f in os.listdir(docs_dir))
    entries.append(ReleaseManifestEntry(path="docs/generated", kind="docs", present=docs_present, checksum=None))

    bench_path = os.path.join(root, "benchmarks", "benchmark.json")
    entries.append(ReleaseManifestEntry(path="benchmarks/benchmark.json", kind="benchmark",
                                         present=os.path.isfile(bench_path), checksum=_checksum(bench_path)))

    return ReleaseManifest(version=version, entries=entries, generated_at=_now_iso())


def write_release_manifest(version: str, root: str = ".", out_path: str = "release/release_manifest.json") -> str:
    manifest = build_release_manifest(version, root)
    target = os.path.join(root, out_path) if not os.path.isabs(out_path) else out_path
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(manifest.model_dump_json(indent=2))
    return target
