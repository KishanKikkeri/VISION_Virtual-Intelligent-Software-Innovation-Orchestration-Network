"""services/security/workers/dependency.py — CVE Scanner (Dependency Scan)."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.models import DependencyScan, FindingSeverity, Vulnerability
from services.security.utils import KNOWN_VULNERABLE_PACKAGES, extract_dependencies_from_source


@AgentFactory.register("cve_scanner_worker")
class CveScannerWorker(BaseAgent):
    """
    Scans all dependencies against a CVE reference table. Deterministic —
    no LLM call needed, mirroring QA's RegressionSuiteWorker/
    PerformanceTestWorker pattern of a deterministic gate over
    Engineering's approved artifacts.

    Design note (docs/M3.5_Security_Service_Handover.md): the platform
    does not yet produce a standalone `dependency_manifest` artifact, so
    the manifest is derived from Engineering's `source_code` artifact
    (requirements.txt / package.json) via
    services.security.utils.extract_dependencies_from_source.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        source = task.context.get_artifact("source_code", {})
        files = source.get("files", []) if isinstance(source, dict) else []
        dependencies = extract_dependencies_from_source(files)

        # Manual override hook (tests / ops), same pattern as QA's
        # __coverage_threshold__ / __perf_p95_override_ms__.
        override = task.context.approved_artifacts.get("__dependency_manifest_override__")
        if override is not None:
            dependencies = override

        vulnerabilities = []
        for dep in dependencies:
            name = str(dep.get("name", "")).lower()
            entry = KNOWN_VULNERABLE_PACKAGES.get(name)
            if entry:
                vulnerabilities.append(Vulnerability(
                    package=dep.get("name", name), version=dep.get("version", "unknown"),
                    cve_id=entry["cve_id"], severity=FindingSeverity(entry["severity"]),
                    description=f"Known-vulnerable version; safe >= {entry['max_safe_version']}",
                ))

        scan = DependencyScan(
            project_id=task.project_id,
            dependencies_scanned=len(dependencies),
            vulnerabilities=vulnerabilities,
        )

        artifact = await self.create_artifact(
            task, "dependency_scan", {**scan.model_dump(), "project_id": task.project_id},
        )
        status = TaskStatus.FAILED if scan.has_critical else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content=scan.model_dump(),
            summary=f"Dependency scan: {len(dependencies)} scanned, "
                    f"{len(vulnerabilities)} vulnerable ({scan.critical_count} critical)",
            quality_score=0.9 if not scan.has_critical else 0.2,
            artifacts=[artifact],
            failure_reason=None if not scan.has_critical
            else f"{scan.critical_count} critical CVE(s) detected in dependencies",
        )
