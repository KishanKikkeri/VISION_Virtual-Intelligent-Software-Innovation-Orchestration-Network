"""
services/integration/production/release_manager.py
=================================
M4.9 §5 Release Manager — semantic versioning, release notes, artifact
and dependency inventory, SBOM integration, upgrade checklist, rollback
plan.

**"Reuse M4.6 SBOM."** This sandbox slice does not include the real
`services.integration.security_hardening.sbom` module (same standing
scope note every M4.x module in this slice carries about milestones not
present in this particular zip — see `docs/M4.9_Production_Readiness_
Handover.md` §3). `attach_sbom` tries that import first and only falls
back to `SBOMReference(available=False, ...)` when it doesn't resolve —
never regenerates SBOM logic locally.
"""
from __future__ import annotations

import importlib
import os
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from services.integration.production.release_models import (
    ArtifactEntry, ChecklistStep, DependencyEntry, Release, SBOMReference, SemanticVersion,
)

_SEMVER_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── semantic versioning ───────────────────────────────────────────

def parse_semver(version: str) -> SemanticVersion:
    match = _SEMVER_RE.match(version.strip())
    if not match:
        raise ValueError(f"{version!r} is not a valid semantic version (expected MAJOR.MINOR.PATCH[-prerelease])")
    return SemanticVersion(major=int(match["major"]), minor=int(match["minor"]), patch=int(match["patch"]),
                            prerelease=match["prerelease"])


def bump_version(version: str, part: str = "patch") -> str:
    semver = parse_semver(version)
    if part == "major":
        semver = SemanticVersion(major=semver.major + 1, minor=0, patch=0)
    elif part == "minor":
        semver = SemanticVersion(major=semver.major, minor=semver.minor + 1, patch=0)
    elif part == "patch":
        semver = SemanticVersion(major=semver.major, minor=semver.minor, patch=semver.patch + 1)
    else:
        raise ValueError(f"part must be one of 'major'/'minor'/'patch', got {part!r}")
    return str(semver)


def is_breaking_upgrade(previous_version: str, next_version: str) -> bool:
    return parse_semver(next_version).major > parse_semver(previous_version).major


# ── inventories ────────────────────────────────────────────────────

def build_artifact_inventory(root: str = ".", package_globs: Optional[Iterable[str]] = None) -> List[ArtifactEntry]:
    """Walks `services/` (and `alembic/versions/` for migrations) under
    `root` and lists Python modules/migrations as release artifacts.
    Purely filesystem-based — no build step is invoked."""
    entries: List[ArtifactEntry] = []
    services_dir = os.path.join(root, "services")
    if os.path.isdir(services_dir):
        for dirpath, _dirs, filenames in os.walk(services_dir):
            for fname in filenames:
                if fname.endswith(".py") and fname != "__init__.py":
                    rel = os.path.relpath(os.path.join(dirpath, fname), root)
                    entries.append(ArtifactEntry(name=fname, kind="module", path=rel))
    migrations_dir = os.path.join(root, "alembic", "versions")
    if os.path.isdir(migrations_dir):
        for fname in sorted(os.listdir(migrations_dir)):
            if fname.endswith(".py"):
                entries.append(ArtifactEntry(name=fname, kind="migration",
                                              path=os.path.relpath(os.path.join(migrations_dir, fname), root)))
    return entries


def build_dependency_inventory(requirements_path: str = "requirements.txt") -> List[DependencyEntry]:
    if not os.path.isfile(requirements_path):
        return []
    entries: List[DependencyEntry] = []
    with open(requirements_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ("==", ">=", "<=", "~="):
                if sep in line:
                    name, _, version = line.partition(sep)
                    entries.append(DependencyEntry(name=name.strip(), version=version.strip(),
                                                     source=requirements_path))
                    break
            else:
                entries.append(DependencyEntry(name=line, version="", source=requirements_path))
    return entries


def attach_sbom() -> SBOMReference:
    try:
        module = importlib.import_module("services.integration.security_hardening.sbom")
        generate = getattr(module, "generate_sbom")
    except Exception as e:  # noqa: BLE001 — not available in this sandbox slice; see module docstring
        return SBOMReference(available=False, note=f"M4.6 SBOM generator not available in this environment: {e}")
    try:
        sbom = generate()
        return SBOMReference(available=True, format=sbom.get("format", "cyclonedx"),
                              component_count=len(sbom.get("components", [])), location=sbom.get("location"))
    except Exception as e:  # noqa: BLE001 — present but failed; degrade rather than fail the release build
        return SBOMReference(available=False, note=f"SBOM generation failed: {e}")


# ── checklists ─────────────────────────────────────────────────────

def build_upgrade_checklist(previous_version: Optional[str], next_version: str) -> List[ChecklistStep]:
    steps = [
        ChecklistStep(step="backup", detail="take a full backup before upgrading", required=True),
        ChecklistStep(step="run_migrations", detail="run `alembic upgrade head`", required=True),
        ChecklistStep(step="validate_environment", detail="run `production check` against the target environment",
                      required=True),
        ChecklistStep(step="validate_deployment_assets", detail="run `production validate` over deployment assets",
                      required=True),
        ChecklistStep(step="smoke_test", detail="exercise the ten production workflows end-to-end", required=True),
    ]
    if previous_version and is_breaking_upgrade(previous_version, next_version):
        steps.insert(0, ChecklistStep(step="review_breaking_changes",
                                       detail=f"{previous_version} -> {next_version} is a major-version upgrade; "
                                              "review release notes for breaking changes",
                                       required=True))
    return steps


def build_rollback_plan(previous_version: Optional[str], next_version: str) -> List[ChecklistStep]:
    if previous_version is None:
        return [ChecklistStep(step="no_prior_release", detail="no previous release recorded; rollback not applicable",
                               required=False)]
    return [
        ChecklistStep(step="stop_traffic", detail="drain/stop traffic to the upgraded deployment", required=True),
        ChecklistStep(step="restore_backup", detail=f"restore the pre-upgrade backup taken before {next_version}",
                       required=True),
        ChecklistStep(step="downgrade_migrations", detail=f"run `alembic downgrade` back toward {previous_version}",
                       required=True),
        ChecklistStep(step="redeploy_previous_artifacts", detail=f"redeploy artifacts for {previous_version}",
                       required=True),
        ChecklistStep(step="verify_health", detail="re-run environment checks and smoke tests", required=True),
    ]


# ── assembly ───────────────────────────────────────────────────────

def build_release(version: str, previous_version: Optional[str] = None, release_notes: Optional[List[str]] = None,
                   channel: str = "stable", root: str = ".") -> Release:
    parse_semver(version)  # validates format; raises ValueError if malformed
    return Release(
        version=version,
        previous_version=previous_version,
        channel=channel,
        release_notes=release_notes or [],
        artifacts=build_artifact_inventory(root),
        dependencies=build_dependency_inventory(os.path.join(root, "requirements.txt")),
        sbom=attach_sbom(),
        upgrade_checklist=build_upgrade_checklist(previous_version, version),
        rollback_plan=build_rollback_plan(previous_version, version),
        created_at=_now_iso(),
    )
