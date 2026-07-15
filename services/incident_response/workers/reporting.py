"""services/incident_response/workers/reporting.py — Reporting Worker.

Deterministic — no LLM call. The only worker that creates
`root_cause_analysis`, `remediation_plan`, and `incident_report`
(spec §12). Computes the incident's final status itself
(utils.final_status_for) since, by the time Communication Lead runs,
Incident Analysis Lead and Recovery Lead have already completed —
Incident Response Head's own `finalize` step (head/__init__.py) simply
persists this decision, it doesn't make it.
"""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.integration.incident_repository import IncidentReportRepository
from services.incident_response.models import (
    IncidentReport, IncidentStatus, RecoveryActionType, RemediationPlan, RootCauseAnalysis,
)
from services.incident_response.utils import final_status_for, summarize_incident
from services.monitoring.models import AlertSeverity, MonitoredComponent

_PREVENTIVE_BY_COMPONENT = {
    MonitoredComponent.DEPLOYMENTS: ["Add a post-deploy health-check gate before marking a deployment successful."],
    MonitoredComponent.REPOSITORY: ["Require CI to pass before merge for this repository."],
}


@AgentFactory.register("reporting_worker")
class ReportingWorker(BaseAgent):
    """Deterministic — no LLM call. Only this worker creates root_cause_analysis,
    remediation_plan, and incident_report."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        incident_id = arts.get("__incident_id__")
        component = MonitoredComponent(arts.get("__component__"))
        severity = AlertSeverity(arts.get("__severity__", "critical"))

        classifier_output = arts.get("incident_classifier_worker", {})
        classification = classifier_output.get("classification", {})
        action = RecoveryActionType(classification.get("recommended_action", "none"))
        evidence = arts.get("evidence_collection_worker", {}).get("evidence", [])
        recovery_output = arts.get("recovery_worker", {})
        recovery_status = recovery_output.get("recovery_status", "skipped")
        recovery_plan = recovery_output.get("recovery_plan")

        final_status = final_status_for(recovery_status, action)
        task.context.approved_artifacts["__final_status__"] = final_status

        root_cause = RootCauseAnalysis(
            incident_id=incident_id,
            probable_cause=classification.get("rationale", "Not enough evidence to determine a probable cause."),
            contributing_factors=[e.get("summary", "") for e in evidence][:5],
            confidence=0.8 if evidence else 0.3,
        )
        remediation = RemediationPlan(
            incident_id=incident_id,
            recommendations=["Review the incident_report and recovery_plan with the on-call operator."],
            preventive_actions=_PREVENTIVE_BY_COMPONENT.get(component, ["Add targeted alerting for this component."]),
        )
        report = IncidentReport(
            incident_id=incident_id, component=component, severity=severity,
            status=IncidentStatus(final_status),
            summary=summarize_incident(component, severity, action, final_status),
            root_cause=root_cause, remediation=remediation,
            recovery_plan=recovery_plan,
        )

        root_cause_artifact = await self.create_artifact(task, "root_cause_analysis", root_cause.model_dump(mode="json"))
        remediation_artifact = await self.create_artifact(task, "remediation_plan", remediation.model_dump(mode="json"))
        report_artifact = await self.create_artifact(task, "incident_report", report.model_dump(mode="json"))

        try:
            async with self._db_factory() as db:
                await IncidentReportRepository.record(
                    db, incident_id, report.summary, root_cause.probable_cause,
                    remediation.model_dump(mode="json"))
        except Exception:
            pass

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"final_status": final_status, "incident_report": report.model_dump(mode="json")},
            summary=f"Report generated: final_status={final_status}",
            quality_score=1.0,
            artifacts=[root_cause_artifact, remediation_artifact, report_artifact],
        )
