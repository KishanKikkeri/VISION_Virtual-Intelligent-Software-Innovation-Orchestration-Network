"""
services/integration/plugin_sdk/plugin_validator.py
=================================
M4.7 §8 Plugin Validation — every function here is a pure predicate/
check over already-parsed `plugin_models.PluginManifest` objects (no
filesystem/import access — that's `plugin_loader.py`'s job). Covers the
brief's full checklist: manifest (via `plugin_manifest.py`, called
before this module ever sees a manifest), duplicate IDs, dependency
graph (missing deps + cyclic deps), permission declarations, hook
names, and version compatibility. "imports" (brief's second bullet) is
necessarily deferred to `plugin_loader.py` (only it actually imports
anything) — see that module's docstring for the corresponding
`validate_entrypoint_importable` check.

**Platform API version compatibility** uses a small, deliberately
independent major/minor comparator (`_parse_version`/
`_is_compatible`) rather than importing `security_hardening.
dependency_scanner`'s `_version_leq` — these are two different
milestones' own pure modules and the brief's "no duplicated utilities"
instruction is about *platform* utilities (repository/dashboard/export/
CLI conventions), not about two independent, single-purpose ~5-line
version comparators; duplicating five lines here avoids a cross-
milestone-package import dependency for something this small.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from services.integration.plugin_sdk.plugin_models import (
    DependencyGraphResult, HookType, Permission, PluginManifest, ValidationIssue, ValidationResult,
)

# The platform's own plugin API version — a manifest's `api_version`
# must be compatible with this (see `_is_compatible`).
PLATFORM_API_VERSION = "1.0.0"

_KNOWN_PERMISSIONS = {p.value for p in Permission}
_KNOWN_HOOKS = {h.value for h in HookType}


def _parse_version(version: str) -> Tuple[int, int, int]:
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    out = []
    for p in parts:
        digits = "".join(ch for ch in p if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)  # type: ignore[return-value]


def _is_compatible(manifest_api_version: str, platform_api_version: str = PLATFORM_API_VERSION) -> bool:
    """A manifest is compatible with the platform when their major
    versions match — the same coarse "major version is the
    compatibility boundary" convention semver-based tooling generally
    uses. Minor/patch differences are never a compatibility error
    (see `validate_version_compatibility`'s warning-vs-error split)."""
    manifest_major = _parse_version(manifest_api_version)[0]
    platform_major = _parse_version(platform_api_version)[0]
    return manifest_major == platform_major


def validate_permissions(manifest: PluginManifest) -> List[ValidationIssue]:
    """Brief §7 "Validator rejects undeclared permissions" — flags any
    permission string in the manifest that isn't one of
    `plugin_models.Permission`'s values."""
    issues: List[ValidationIssue] = []
    for perm in manifest.permissions:
        if perm not in _KNOWN_PERMISSIONS:
            issues.append(ValidationIssue(
                rule_id="PLUGIN-UNKNOWN-PERMISSION", severity="error", plugin_id=manifest.id,
                message=f"permission {perm!r} is not a recognized permission "
                        f"(known: {sorted(_KNOWN_PERMISSIONS)})",
            ))
    return issues


def validate_hooks(manifest: PluginManifest) -> List[ValidationIssue]:
    """Flags any hook name in the manifest that isn't one of
    `plugin_models.HookType`'s values — brief's "missing hooks" check,
    read as "the manifest names a hook the platform doesn't recognize"
    (the complementary case — an entrypoint module missing a function
    for a hook it *did* declare — is `plugin_runtime.py`'s concern at
    dispatch time, since only it actually inspects the loaded module;
    see that module's `dispatch_hook` docstring)."""
    issues: List[ValidationIssue] = []
    for hook in manifest.hooks:
        if hook not in _KNOWN_HOOKS:
            issues.append(ValidationIssue(
                rule_id="PLUGIN-UNKNOWN-HOOK", severity="error", plugin_id=manifest.id,
                message=f"hook {hook!r} is not a recognized hook (known: {sorted(_KNOWN_HOOKS)})",
            ))
    return issues


def validate_version_compatibility(manifest: PluginManifest,
                                    platform_api_version: str = PLATFORM_API_VERSION) -> List[ValidationIssue]:
    if _is_compatible(manifest.api_version, platform_api_version):
        return []
    return [ValidationIssue(
        rule_id="PLUGIN-API-VERSION-INCOMPATIBLE", severity="error", plugin_id=manifest.id,
        message=f"plugin api_version {manifest.api_version!r} is not compatible with platform "
                f"api_version {platform_api_version!r} (major version must match)",
    )]


def validate_duplicate_ids(manifests: List[PluginManifest]) -> Dict[str, List[ValidationIssue]]:
    """Cross-plugin check — flags every plugin id that appears more
    than once across `manifests`. Returns a dict keyed by plugin id
    (matching `validate_all`'s per-plugin result shape) rather than a
    flat list, so a duplicate is visible on every one of its own
    colliding entries."""
    seen: Dict[str, int] = {}
    for m in manifests:
        seen[m.id] = seen.get(m.id, 0) + 1
    issues_by_id: Dict[str, List[ValidationIssue]] = {}
    for plugin_id, count in seen.items():
        if count > 1:
            issues_by_id[plugin_id] = [ValidationIssue(
                rule_id="PLUGIN-DUPLICATE-ID", severity="error", plugin_id=plugin_id,
                message=f"plugin id {plugin_id!r} is declared by {count} plugins; ids must be unique",
            )]
    return issues_by_id


def build_dependency_graph(manifests: List[PluginManifest]) -> DependencyGraphResult:
    """Brief §8 "dependency graph" + "cyclic dependencies" checks in
    one pass: a topological sort (Kahn's algorithm) over the declared
    `dependencies` edges. Missing dependencies (a plugin depends on an
    id not present in `manifests`) are reported separately in
    `missing_dependencies` and do not by themselves prevent computing
    an order for the rest of the graph; a genuine cycle does prevent a
    full order — `order` will be shorter than `manifests` whenever
    `cycles` is non-empty (the nodes involved in a cycle, and anything
    depending on them, are omitted from `order` rather than guessed
    at)."""
    by_id = {m.id: m for m in manifests}
    missing: Dict[str, List[str]] = {}
    edges: Dict[str, List[str]] = {m.id: [] for m in manifests}  # dependent -> [dependency ids present in graph]
    in_degree: Dict[str, int] = {m.id: 0 for m in manifests}

    for m in manifests:
        for dep in m.dependencies:
            if dep.plugin_id not in by_id:
                missing.setdefault(m.id, []).append(dep.plugin_id)
                continue
            edges[dep.plugin_id].append(m.id)  # dep must load before m
            in_degree[m.id] += 1

    queue = sorted([pid for pid, deg in in_degree.items() if deg == 0])
    order: List[str] = []
    remaining_in_degree = dict(in_degree)
    while queue:
        queue.sort()
        node = queue.pop(0)
        order.append(node)
        for dependent in sorted(edges[node]):
            remaining_in_degree[dependent] -= 1
            if remaining_in_degree[dependent] == 0:
                queue.append(dependent)

    cycles: List[List[str]] = []
    if len(order) < len(manifests):
        unresolved = sorted(set(by_id) - set(order))
        cycles = _find_cycles(unresolved, edges)

    return DependencyGraphResult(order=order, cycles=cycles, missing_dependencies=missing)


def _find_cycles(unresolved: List[str], edges: Dict[str, List[str]]) -> List[List[str]]:
    """Finds simple cycles among `unresolved` nodes (the set Kahn's
    algorithm couldn't place) via DFS. Deterministic: nodes/neighbors
    are always visited in sorted order, so the same graph always
    produces the same reported cycle list."""
    unresolved_set = set(unresolved)
    cycles: List[List[str]] = []
    visited: set = set()

    def dfs(node: str, path: List[str], on_path: set) -> None:
        if node in on_path:
            cycle_start = path.index(node)
            cycle = path[cycle_start:]
            normalized = tuple(sorted(cycle))
            if normalized not in {tuple(sorted(c)) for c in cycles}:
                cycles.append(cycle)
            return
        if node in visited:
            return
        visited.add(node)
        path.append(node)
        on_path.add(node)
        for neighbor in sorted(edges.get(node, [])):
            if neighbor in unresolved_set:
                dfs(neighbor, path, on_path)
        path.pop()
        on_path.discard(node)

    for node in sorted(unresolved):
        if node not in visited:
            dfs(node, [], set())
    return cycles


def validate_manifest(manifest: PluginManifest, platform_api_version: str = PLATFORM_API_VERSION) -> ValidationResult:
    """Single-plugin checks only (permissions/hooks/version
    compatibility) — see `validate_all` for the cross-plugin checks
    (duplicate ids, dependency graph) that need the full manifest set."""
    issues = [
        *validate_permissions(manifest), *validate_hooks(manifest),
        *validate_version_compatibility(manifest, platform_api_version),
    ]
    return ValidationResult(plugin_id=manifest.id, valid=not any(i.severity == "error" for i in issues), issues=issues)


def validate_all(manifests: List[PluginManifest],
                  platform_api_version: str = PLATFORM_API_VERSION) -> Dict[str, ValidationResult]:
    """Brief §8 entry point: every check, single-plugin and
    cross-plugin, folded into one `ValidationResult` per plugin id."""
    duplicate_issues = validate_duplicate_ids(manifests)
    graph = build_dependency_graph(manifests)

    results: Dict[str, ValidationResult] = {}
    for m in manifests:
        result = validate_manifest(m, platform_api_version)
        issues = list(result.issues) + duplicate_issues.get(m.id, [])

        if m.id in graph.missing_dependencies:
            for missing_dep in graph.missing_dependencies[m.id]:
                issues.append(ValidationIssue(
                    rule_id="PLUGIN-MISSING-DEPENDENCY", severity="error", plugin_id=m.id,
                    message=f"plugin {m.id!r} depends on {missing_dep!r}, which is not installed",
                ))
        for cycle in graph.cycles:
            if m.id in cycle:
                issues.append(ValidationIssue(
                    rule_id="PLUGIN-CYCLIC-DEPENDENCY", severity="error", plugin_id=m.id,
                    message=f"plugin {m.id!r} is part of a dependency cycle: {' -> '.join(cycle + [cycle[0]])}",
                ))

        results[m.id] = ValidationResult(
            plugin_id=m.id, valid=not any(i.severity == "error" for i in issues), issues=issues,
        )
    return results
