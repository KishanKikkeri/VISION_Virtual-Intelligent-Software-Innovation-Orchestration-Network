"""
services/integration/release_validation/
=================================
M4.10 — Final Platform Release & Launch. See
docs/M4.10_Final_Release_Handover.md for the full writeup.

Release-readiness tooling layered on top of the now feature-complete
AASC platform (M3.1-M4.9). Nothing here is a new runtime concept — this
package scores, audits, and packages the *existing* platform for a
v1.0.0 Release Candidate tag. Every module degrades gracefully whenever
optional infrastructure or a sibling M4.x package isn't importable in
the current environment, matching M4.9's `production/` package
convention.

    release_validation_models.py  Pure Pydantic shapes (ReleaseScore, CompatibilityMatrix,
                                  DependencyReport, EnvironmentAuditReport, BenchmarkSummaryReport,
                                  ReadinessReport, QAReport, ReleaseManifest, InstallReport, ...).
    compatibility_matrix.py       Component version compatibility (Python/Postgres/Redis/NATS/Docker).
    dependency_checker.py         Missing/conflicting/duplicate/unpinned requirements.txt entries.
    environment_audit.py          Thin adapter over M4.9's environment_validator.
    benchmark_summary.py          Turns raw benchmark timings into a report + regression detection.
    release_validator.py          Weighted release-score aggregator.
    readiness_report.py           Orchestrates the above into one unified ReadinessReport.
    release_export.py             json/markdown/html export, reusing M4.9's generic renderer.
    documentation_generator.py    Generates docs/generated/*.md from platform metadata.
    final_qa.py                   Runs workflow/lint/replay/chaos/security/production/plugin checks.
    release_packaging.py          CHANGELOG/RELEASE_NOTES/LICENSE/NOTICE/VERSION + release_manifest.json.
    release_cli.py                python release_cli.py release [check|docs|install-check|benchmark|qa|package]
"""
