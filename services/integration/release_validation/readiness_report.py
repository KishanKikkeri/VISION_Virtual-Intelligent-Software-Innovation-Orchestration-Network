"""
services/integration/release_validation/readiness_report.py
=================================
M4.10 §1 Release Validation — orchestrates `dependency_checker`,
`compatibility_matrix`, `environment_audit`, `benchmark_summary`,
`release_validator`, and a documentation-completeness scan into one
`ReadinessReport` (the "unified Release Report" §1 asks for). This is
the module `release_cli.py` and `/platform/release/*` call; every
sub-module above stays independently callable/testable.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.integration.release_validation.benchmark_summary import build_benchmark_summary, detect_regressions
from services.integration.release_validation.compatibility_matrix import build_compatibility_matrix
from services.integration.release_validation.dependency_checker import check_dependencies
from services.integration.release_validation.environment_audit import run_audit
from services.integration.release_validation.release_validation_models import (
    DocumentationCompleteness, ReadinessReport,
)
from services.integration.release_validation.release_validator import compute_release_score

# §2's ten generated documents, verbatim.
EXPECTED_DOCS = [
    "Architecture.md", "API.md", "Workflows.md", "Agents.md", "Events.md", "Database.md", "Plugins.md",
    "Deployment.md", "Security.md", "Operations.md",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scan_documentation_completeness(docs_generated_dir: str = "docs/generated",
                                     expected: Optional[List[str]] = None) -> DocumentationCompleteness:
    expected = expected or EXPECTED_DOCS
    present: List[str] = []
    if os.path.isdir(docs_generated_dir):
        for name in expected:
            path = os.path.join(docs_generated_dir, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                present.append(name)
    return DocumentationCompleteness(expected_documents=expected, present_documents=present)


def build_readiness_report(version: str, root: str = ".", requirements_path: str = "requirements.txt",
                            docs_generated_dir: str = "docs/generated",
                            detected_versions: Optional[Dict[str, str]] = None,
                            environment: str = "production",
                            benchmark_results: Optional[Dict[str, float]] = None,
                            benchmark_baseline: Optional[Dict[str, float]] = None,
                            environment_probes: Optional[Dict[str, Optional[Callable[[], Any]]]] = None) -> ReadinessReport:
    """The single entry point every layer above (CLI/API) calls.
    Every argument has a safe default so this is callable with zero
    configuration (matching M4.9's "always safe to call with zero
    arguments" convention for `run_environment_checks`) — it just
    produces a mostly-degraded report in that case."""
    environment_probes = environment_probes or {}

    dependency_report = check_dependencies(os.path.join(root, requirements_path)
                                            if not os.path.isabs(requirements_path) else requirements_path)
    compatibility = build_compatibility_matrix(detected_versions)
    audit = run_audit(environment, **environment_probes)
    documentation = scan_documentation_completeness(os.path.join(root, docs_generated_dir)
                                                      if not os.path.isabs(docs_generated_dir) else docs_generated_dir)
    benchmarks = build_benchmark_summary(benchmark_results or {}, baseline=benchmark_baseline)
    regressions = detect_regressions(benchmarks)

    score = compute_release_score(dependency_report, compatibility, audit, documentation, regressions)

    return ReadinessReport(
        version=version, release_score=score, dependency_report=dependency_report,
        compatibility_matrix=compatibility, environment_audit=audit, benchmark_summary=benchmarks,
        documentation_completeness=documentation, generated_at=_now_iso(),
    )
