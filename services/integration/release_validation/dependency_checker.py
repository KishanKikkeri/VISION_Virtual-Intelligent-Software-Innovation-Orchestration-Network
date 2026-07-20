"""
services/integration/release_validation/dependency_checker.py
=================================
M4.10 §1 Release Validation — dependency report: missing / conflicting /
duplicate / unpinned entries in `requirements.txt`. Reuses M4.9's
`release_manager.build_dependency_inventory` for parsing (per this
milestone's mission: "do not redesign existing architecture") instead of
re-implementing a requirements-file parser here.
"""
from __future__ import annotations

from collections import defaultdict
from typing import List, Optional

from services.integration.release_validation.release_validation_models import (
    DependencyIssue, DependencyReport, Severity,
)

try:
    from services.integration.production.release_manager import build_dependency_inventory
except Exception:  # noqa: BLE001 — production package not importable in this context; degrade, don't crash
    build_dependency_inventory = None  # type: ignore[assignment]


def check_dependencies(requirements_path: str = "requirements.txt") -> DependencyReport:
    if build_dependency_inventory is None:
        return DependencyReport(total_dependencies=0, issues=[
            DependencyIssue(name="requirements.txt", kind="missing",
                             detail="M4.9 release_manager.build_dependency_inventory not importable",
                             severity=Severity.WARNING),
        ])
    entries = build_dependency_inventory(requirements_path)
    if not entries:
        return DependencyReport(total_dependencies=0, issues=[
            DependencyIssue(name=requirements_path, kind="missing",
                             detail=f"{requirements_path} not found or empty", severity=Severity.WARNING),
        ])

    issues: List[DependencyIssue] = []
    by_name = defaultdict(list)
    for entry in entries:
        by_name[entry.name.lower()].append(entry)

    for name, versions in by_name.items():
        if len(versions) > 1:
            distinct = {v.version for v in versions}
            if len(distinct) > 1:
                issues.append(DependencyIssue(
                    name=name, kind="version_conflict",
                    detail=f"{len(versions)} entries with differing versions: {sorted(distinct)}",
                    severity=Severity.ERROR,
                ))
            else:
                issues.append(DependencyIssue(
                    name=name, kind="duplicate", detail=f"listed {len(versions)} times",
                    severity=Severity.WARNING,
                ))
        for v in versions:
            if not v.version:
                issues.append(DependencyIssue(
                    name=name, kind="unpinned", detail=f"{name} has no pinned version",
                    severity=Severity.WARNING,
                ))

    return DependencyReport(total_dependencies=len(entries), issues=issues)


def diff_against_lockfile(requirements_path: str, lock_versions: Optional[dict] = None) -> DependencyReport:
    """Optional stricter check: flags any dependency whose declared
    version disagrees with a caller-supplied lockfile snapshot
    (`{name: version}`). `lock_versions=None` degrades to the plain
    `check_dependencies` result — no lockfile format is assumed."""
    report = check_dependencies(requirements_path)
    if not lock_versions or build_dependency_inventory is None:
        return report
    entries = build_dependency_inventory(requirements_path)
    extra_issues = list(report.issues)
    for entry in entries:
        locked = lock_versions.get(entry.name)
        if locked and entry.version and locked != entry.version:
            extra_issues.append(DependencyIssue(
                name=entry.name, kind="version_conflict",
                detail=f"requirements.txt pins {entry.version}, lockfile pins {locked}",
                severity=Severity.ERROR,
            ))
    return DependencyReport(total_dependencies=report.total_dependencies, issues=extra_issues)
