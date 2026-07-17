"""
services/integration/diagnostics/graph_linter.py
=================================
CI/CD & Quality Gates §3 — "graph linting and architecture checks."

Deliberately separate from `workflow_validator.WorkflowReport`:
`WorkflowReport.healthy` answers "does this graph actually work at
runtime" (reachability, END, entry point). This module answers "does
this graph follow the platform's architectural conventions" — a graph
can be perfectly *healthy* and still fail lint (e.g. a routing
function written and imported but never wired into any
`add_conditional_edges` call, which is exactly the class of bug fixed
in QA's `dlq` node during M3.10 — LINT-E005 exists specifically to
catch that class of bug automatically from now on).

Each `LintFinding` has a stable `rule_id` so CI output, PR annotations,
and this module's own tests can all refer to "did LINT-E005 fire",
independent of the human-readable message wording.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel

from services.integration import lifecycle
from services.integration.validators.workflow_validator import WorkflowReport, analyze_workflow

_NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_IMPORT_ROUTE_FN_RE = re.compile(
    r"from\s+[\w.]+\.routing(?:\s+import\s+\(([^)]*)\)|\s+import\s+([^\n]*))", re.DOTALL)
_ROUTE_NAME_RE = re.compile(r"\broute_\w+\b")
MAX_RECOMMENDED_DEPTH = 25


class LintFinding(BaseModel):
    rule_id: str
    severity: str  # "error" | "warning"
    workflow: str
    node: Optional[str] = None
    message: str


class LintReport(BaseModel):
    workflow: str
    findings: List[LintFinding] = []

    @property
    def errors(self) -> List[LintFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[LintFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


def _imported_route_functions(source: str) -> List[str]:
    names: List[str] = []
    for m in _IMPORT_ROUTE_FN_RE.finditer(source):
        block = m.group(1) or m.group(2) or ""
        names.extend(_ROUTE_NAME_RE.findall(block))
    return sorted(set(names))


def _used_route_functions(source: str) -> List[str]:
    """Route-prefixed function names actually passed as the 2nd argument
    to add_conditional_edges(...) anywhere in the module."""
    used = set()
    for m in re.finditer(r"add_conditional_edges\(\s*[\"'][^\"']+[\"']\s*,\s*(\w+)\s*,", source):
        used.add(m.group(1))
    return sorted(used)


def lint_workflow(name: str, report: WorkflowReport, source_module=None) -> LintReport:
    """Never raises — a lint pass that can't inspect source text (e.g. a
    workflow that failed to build at all) just reports what it can from
    `report` alone."""
    findings: List[LintFinding] = []

    if not report.built:
        findings.append(LintFinding(rule_id="LINT-E001", severity="error", workflow=name,
                                     message=f"workflow failed to build: {report.error}"))
        return LintReport(workflow=name, findings=findings)

    if report.entry_node is None:
        findings.append(LintFinding(rule_id="LINT-E002", severity="error", workflow=name,
                                     message="no entry node found from __start__"))
    if report.finish_node is None:
        findings.append(LintFinding(rule_id="LINT-E003", severity="error", workflow=name,
                                     message="graph has no __end__ node"))
    elif "__end__" not in report.reachable_nodes:
        findings.append(LintFinding(rule_id="LINT-E004", severity="error", workflow=name,
                                     message="__end__ is not reachable from __start__"))
    for node in report.unreachable_nodes:
        findings.append(LintFinding(rule_id="LINT-E005", severity="error", workflow=name, node=node,
                                     message=f"node {node!r} is unreachable from __start__"))
    for node in report.isolated_nodes:
        findings.append(LintFinding(rule_id="LINT-W001", severity="warning", workflow=name, node=node,
                                     message=f"node {node!r} has no edges at all (isolated)"))
    if report.dead_end_count:
        findings.append(LintFinding(rule_id="LINT-W002", severity="warning", workflow=name,
                                     message=f"{report.dead_end_count} node(s) have no outgoing route"))
    for route in report.conditional_routes:
        for outcome, target in route.outcomes.items():
            all_nodes = set(report.reachable_nodes) | set(report.unreachable_nodes) | set(report.isolated_nodes)
            if target not in all_nodes and target != "__end__":
                findings.append(LintFinding(
                    rule_id="LINT-E006", severity="error", workflow=name, node=route.source,
                    message=f"conditional route {route.source!r}/{outcome!r} targets unknown node {target!r}"))
        if not route.function or not route.function.startswith("route_"):
            findings.append(LintFinding(
                rule_id="LINT-W003", severity="warning", workflow=name, node=route.source,
                message=f"routing function {route.function!r} at node {route.source!r} doesn't follow "
                        f"the `route_*` naming convention"))

    for node in set(report.reachable_nodes) - {"__start__", "__end__"}:
        if not _NODE_NAME_RE.match(node):
            findings.append(LintFinding(rule_id="LINT-W004", severity="warning", workflow=name, node=node,
                                         message=f"node name {node!r} doesn't follow snake_case convention"))

    if report.graph_depth > MAX_RECOMMENDED_DEPTH:
        findings.append(LintFinding(
            rule_id="LINT-W005", severity="warning", workflow=name,
            message=f"graph depth {report.graph_depth} exceeds the recommended maximum "
                    f"of {MAX_RECOMMENDED_DEPTH} stages — consider decomposing"))

    if source_module is not None:
        try:
            src = inspect.getsource(source_module)
        except Exception:  # noqa: BLE001
            src = ""
        if src:
            imported = set(_imported_route_functions(src))
            used = set(_used_route_functions(src))
            for orphan in sorted(imported - used):
                findings.append(LintFinding(
                    rule_id="LINT-E007", severity="error", workflow=name, node=orphan,
                    message=f"routing function {orphan!r} is imported but never passed to "
                            f"add_conditional_edges() — this is exactly the class of bug that left "
                            f"QA's dlq node unreachable pre-M3.10; either wire it in or remove the "
                            f"unused import"))

    return LintReport(workflow=name, findings=findings)


def lint_all_workflows() -> Dict[str, LintReport]:
    results: Dict[str, LintReport] = {}
    for entry in lifecycle._graph_registry():  # noqa: SLF001
        name = entry["name"]
        report = analyze_workflow(name, entry.get("builder"), entry.get("kwargs"),
                                   source_module=entry.get("module"), import_error=entry.get("import_error"))
        results[name] = lint_workflow(name, report, source_module=entry.get("module"))
    return results


# ══════════════════════════════════════════════════════════════
# Baseline — tracked, pre-existing findings that shouldn't block CI
# ══════════════════════════════════════════════════════════════
# Turning a new lint rule on for the first time in an established
# codebase routinely surfaces pre-existing debt (see
# docs/M3.10_CI_and_Explorer_Notes.md — LINT-E007 found 3 more
# orphaned routing functions beyond the QA `dlq` one already fixed in
# M3.10: engineering.route_after_aggregate, security.route_after_fan_out,
# qa.route_after_coverage, qa.route_after_execute). The right response
# is neither "silently ignore the rule" nor "fail every build until
# someone does the same source-design archaeology as the dlq fix" —
# it's a baseline: today's known findings are recorded once, CI passes
# against them, and any *new* finding (a different rule, a different
# workflow, or a genuinely new orphaned function) still fails the
# build immediately.

BASELINE_PATH = Path(__file__).with_name("lint_baseline.json")


def _finding_key(f: LintFinding) -> tuple:
    return (f.workflow, f.rule_id, f.node)


def load_baseline(path: Path = BASELINE_PATH) -> List[tuple]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [(e["workflow"], e["rule_id"], e.get("node")) for e in data.get("accepted_findings", [])]


def split_against_baseline(
    reports: Dict[str, LintReport], baseline: Optional[List[tuple]] = None,
) -> "BaselineComparison":
    baseline = load_baseline() if baseline is None else baseline
    baseline_set = set(baseline)
    known: List[LintFinding] = []
    new: List[LintFinding] = []
    for report in reports.values():
        for f in report.errors:
            (known if _finding_key(f) in baseline_set else new).append(f)
    return BaselineComparison(known_findings=known, new_findings=new)


class BaselineComparison(BaseModel):
    known_findings: List[LintFinding]
    new_findings: List[LintFinding]

    @property
    def ci_should_pass(self) -> bool:
        """CI gate: fails only on findings NOT already in the baseline."""
        return len(self.new_findings) == 0
