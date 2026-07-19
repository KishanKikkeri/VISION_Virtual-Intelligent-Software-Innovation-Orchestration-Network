#!/usr/bin/env python3
"""
scripts/ci/validate_workflows.py
=================================
CI/CD Quality Gate — "Add workflow validation to CI" +
"Enforce graph linting and architecture checks on pull requests."

Runs workflow_validator (health: does every graph build, is everything
reachable, does it reach END) and graph_linter (architecture/style
rules, including the LINT-E007 "orphaned routing function" check that
would have caught the QA `dlq` bug automatically) over all 10
registered workflows, prints a human-readable report, and exits
non-zero if:
  - any workflow is unhealthy (fails to build, has a real unreachable
    node, can't reach END), or
  - any lint finding is a NEW error not already recorded in
    services/integration/diagnostics/lint_baseline.json.

Usage:
    python scripts/ci/validate_workflows.py            # health + lint gate
    python scripts/ci/validate_workflows.py --lint-only
    python scripts/ci/validate_workflows.py --health-only
    python scripts/ci/validate_workflows.py --fail-on-warnings

Exit codes: 0 = all gates passed. 1 = a gate failed. 2 = the script
itself crashed (import error, etc) — distinguished from "1" so CI
dashboards can tell "the platform is broken" from "this checker is
broken" at a glance.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lint-only", action="store_true")
    parser.add_argument("--health-only", action="store_true")
    parser.add_argument("--fail-on-warnings", action="store_true",
                         help="also fail the build on any warning-severity finding")
    args = parser.parse_args()

    try:
        from services.integration.diagnostics import graph_linter
        from services.integration.validators import workflow_validator
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: could not import validator/linter modules: {e}", file=sys.stderr)
        return 2

    ok = True
    warning_count = 0

    if not args.lint_only:
        print("=" * 72)
        print("WORKFLOW HEALTH VALIDATION")
        print("=" * 72)
        reports = workflow_validator.validate_all_workflows_detailed()
        for name in sorted(reports):
            r = reports[name]
            status = "PASS" if r.healthy else "FAIL"
            print(f"  [{status}] {name:<20} nodes={r.node_count:<3} edges={r.edge_count:<3} "
                  f"depth={r.graph_depth:<3} warnings={len(r.warnings)} errors={len(r.errors)}")
            for e in r.errors:
                print(f"           ERROR: {e}")
            for w in r.warnings:
                print(f"           warning: {w}")
                warning_count += 1
            if not r.healthy:
                ok = False
        print()

    if not args.health_only:
        print("=" * 72)
        print("GRAPH LINT (architecture / style rules)")
        print("=" * 72)
        lint_reports = graph_linter.lint_all_workflows()
        comparison = graph_linter.split_against_baseline(lint_reports)

        for name in sorted(lint_reports):
            lr = lint_reports[name]
            if not lr.findings:
                print(f"  [PASS] {name:<20} clean")
                continue
            print(f"  [{'PASS' if lr.passed else 'FAIL'}] {name:<20} "
                  f"{len(lr.errors)} error(s), {len(lr.warnings)} warning(s)")
            for f in lr.findings:
                tag = "(baselined)" if (name, f.rule_id, f.node) in {
                    (bf.workflow, bf.rule_id, bf.node) for bf in comparison.known_findings} else ""
                print(f"           {f.rule_id} {f.severity}: {f.message} {tag}")
                if f.severity == "warning":
                    warning_count += 1

        print()
        print(f"  New (non-baselined) error findings: {len(comparison.new_findings)}")
        print(f"  Known/baselined findings:            {len(comparison.known_findings)}")
        if comparison.new_findings:
            ok = False
        print()

    if args.fail_on_warnings and warning_count:
        print(f"--fail-on-warnings set and {warning_count} warning(s) found.")
        ok = False

    print("=" * 72)
    print("RESULT:", "PASS" if ok else "FAIL")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
