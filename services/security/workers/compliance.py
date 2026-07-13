"""
services/security/workers/compliance.py — Compliance Validator Worker.

Produces the spec's SBOM, License Report, and Compliance Report — all
three "Compliance Lead" responsibilities collapsed into the single
registered `compliance_validator_worker` (see docs/M3.5 handover,
"Department Structure" deviation note).
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.models import ComplianceReport, LicenseReport, SBOM, SBOMComponent
from services.security.utils import LICENSE_TABLE, classify_license, extract_dependencies_from_source

DEFAULT_REQUIRED_CHECKLIST = (
    "security_architecture_reviewed",
    "openapi_documented",
    "database_schema_defined",
)


@AgentFactory.register("compliance_validator_worker")
class ComplianceValidatorWorker(BaseAgent):
    """
    Validates the compliance checklist against project requirements,
    generates the SBOM, and classifies dependency licenses. Deterministic —
    no LLM call needed.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        source = task.context.get_artifact("source_code", {})
        files = source.get("files", []) if isinstance(source, dict) else []
        dependencies = extract_dependencies_from_source(files)
        override = task.context.approved_artifacts.get("__dependency_manifest_override__")
        if override is not None:
            dependencies = override

        # ── SBOM ────────────────────────────────────────────────
        components = []
        licenses_found: dict = {}
        disallowed = []
        for dep in dependencies:
            name = str(dep.get("name", "")).lower()
            license_name = LICENSE_TABLE.get(name, "unknown")
            components.append(SBOMComponent(
                name=dep.get("name", name), version=dep.get("version", "unknown"),
                ecosystem=dep.get("ecosystem", "unknown"), license=license_name,
            ))
            licenses_found[license_name] = licenses_found.get(license_name, 0) + 1
            if classify_license(license_name) == "disallowed":
                disallowed.append(license_name)

        sbom = SBOM(project_id=task.project_id, components=components)
        license_report = LicenseReport(
            project_id=task.project_id, licenses_found=licenses_found,
            disallowed_licenses=sorted(set(disallowed)),
        )

        # ── Compliance checklist ─────────────────────────────────
        required = task.context.approved_artifacts.get(
            "__compliance_checklist_override__", list(DEFAULT_REQUIRED_CHECKLIST))
        checklist = {
            "security_architecture_reviewed": bool(task.context.get_artifact("security_architecture")),
            "openapi_documented": bool(task.context.get_artifact("openapi_spec")),
            "database_schema_defined": bool(task.context.get_artifact("database_schema")),
        }
        violations = [k for k in required if not checklist.get(k, False)]
        compliance_report = ComplianceReport(
            project_id=task.project_id, checklist=checklist, violations=violations,
        )

        sbom_artifact = await self.create_artifact(
            task, "sbom", {**sbom.model_dump(), "project_id": task.project_id})
        license_artifact = await self.create_artifact(
            task, "license_report", {**license_report.model_dump(), "project_id": task.project_id})
        compliance_artifact = await self.create_artifact(
            task, "compliance_report", {**compliance_report.model_dump(), "project_id": task.project_id})

        passed = license_report.compliant and compliance_report.compliant
        status = TaskStatus.COMPLETED if passed else TaskStatus.FAILED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={
                "sbom": sbom.model_dump(), "license_report": license_report.model_dump(),
                "compliance_report": compliance_report.model_dump(),
            },
            summary=f"Compliance: {sbom.component_count} component(s), "
                    f"{len(license_report.disallowed_licenses)} disallowed license(s), "
                    f"{len(violations)} policy violation(s)",
            quality_score=0.9 if passed else 0.3,
            artifacts=[sbom_artifact, license_artifact, compliance_artifact],
            failure_reason=None if passed else
            f"disallowed_licenses={license_report.disallowed_licenses}, violations={violations}",
        )
