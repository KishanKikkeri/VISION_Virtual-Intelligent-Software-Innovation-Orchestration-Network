"""
services/integration/artifact_validator.py
=================================
Validates that every required artifact exists before a department is
allowed to run (spec §3). REQUIRED_ARTIFACTS below is this module's
own source of truth (used for validation independent of any one
service's internals), cross-checked at import time against
`services.manager.main`'s `DEPARTMENT_ARTIFACT_TYPES` dict — the two
are expected to agree; `cross_check_with_manager_manifest()` reports
any drift rather than assuming it silently.

Reconnaissance finding (fixed, see docs/M3.9_Platform_Integration_
Handover.md "Genuine bugs fixed"): before this milestone,
DEPARTMENT_ARTIFACT_TYPES had no "qa"/"security" entries even though
both services/qa/head/__init__.py and services/security/head/__init__.py
declare `REQUIRED_ENGINEERING_ARTIFACTS = ("source_code",)` and read it
via `task.context.get_artifact("source_code", {})` — a Manager-delegated
QA/Security task silently fell back to product's artifact list and
never received source_code. Fixed with a 2-line additive change to
services/manager/main.py, the same class of fix already applied once
before for "devops" (see that file's own comment).
"""
from __future__ import annotations

from typing import Dict, List, Set

from pydantic import BaseModel, Field

# This module's source of truth for "what must already exist before X runs".
# Matches services.manager.main.DEPARTMENT_ARTIFACT_TYPES exactly as of
# this milestone (both entries fixed together, see module docstring).
REQUIRED_ARTIFACTS: Dict[str, List[str]] = {
    "product":            [],
    "architecture":       ["feature_spec_doc", "requirements_doc",
                            "user_stories_doc", "acceptance_criteria"],
    "engineering":        ["architecture_blueprint", "openapi_spec", "database_schema",
                            "deployment_architecture", "ui_blueprint"],
    "qa":                 ["source_code"],
    "security":           ["source_code"],
    "devops":             ["source_code", "qa_report", "security_report", "deployment_plan",
                            "dockerfile", "docker_compose", "environment_config",
                            "pipeline_config", "openapi_spec", "database_schema"],
    "monitoring":         [],  # platform-wide, not project-artifact-gated
    "incident_response":  [],  # triggered by monitoring.incident, not an artifact gate
}

# Artifact types each department is known to *produce* (used for
# building "available" sets in tests/reports without a live DB).
PRODUCED_ARTIFACTS: Dict[str, List[str]] = {
    "product":            ["feature_spec_doc", "requirements_doc", "user_stories_doc", "acceptance_criteria"],
    "architecture":       ["architecture_blueprint", "openapi_spec", "database_schema",
                            "deployment_architecture", "ui_blueprint"],
    "engineering":        ["source_code", "pull_request_ref"],
    "qa":                 ["qa_report"],
    "security":           ["security_report"],
    "devops":             ["deployment_plan", "dockerfile", "docker_compose",
                            "environment_config", "pipeline_config"],
    "monitoring":         ["incident_candidate", "dashboard_configuration"],
    "incident_response":  ["incident_report", "incident_timeline", "recovery_plan",
                            "root_cause_analysis", "remediation_plan"],
}


class ArtifactCheckResult(BaseModel):
    department: str
    passed: bool
    required: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)


def required_artifacts_for(department: str) -> List[str]:
    return list(REQUIRED_ARTIFACTS.get(department, []))


def validate_artifacts(department: str, available_artifact_types: Set[str]) -> ArtifactCheckResult:
    """Deterministic — spec §3: 'Produce clear validation errors.'"""
    required = set(REQUIRED_ARTIFACTS.get(department, []))
    missing = sorted(required - available_artifact_types)
    return ArtifactCheckResult(
        department=department, passed=not missing,
        required=sorted(required), missing=missing,
    )


def cross_check_with_manager_manifest() -> Dict[str, List[str]]:
    """
    Compares this module's REQUIRED_ARTIFACTS against the live
    services.manager.main.DEPARTMENT_ARTIFACT_TYPES dict. Returns a
    dict of {department: [discrepancy descriptions]} — empty if they
    fully agree. Never raises; a manager import failure is itself
    reported as a discrepancy rather than crashing the validator.
    """
    discrepancies: Dict[str, List[str]] = {}
    try:
        import inspect
        import re

        import services.manager.main as manager_main
        src = inspect.getsource(manager_main._run_department_pipeline)
        match = re.search(r"DEPARTMENT_ARTIFACT_TYPES:\s*Dict\[str,\s*list\]\s*=\s*\{(.*?)\n\s*\}",
                           src, re.DOTALL)
        manager_depts = set(re.findall(r'"(\w+)":\s*\[', match.group(1))) if match else set()
    except Exception as e:
        return {"__manager_import__": [f"Could not inspect services.manager.main: {e}"]}

    for dept in REQUIRED_ARTIFACTS:
        if dept in ("monitoring", "incident_response"):
            continue  # never delegated via Manager (event-driven/scheduled, see M3.7/M3.8 handovers)
        if dept not in manager_depts:
            discrepancies.setdefault(dept, []).append(
                f"{dept!r} has no DEPARTMENT_ARTIFACT_TYPES entry in services/manager/main.py")

    return discrepancies
