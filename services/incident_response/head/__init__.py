"""
services/incident_response/head — L3 IncidentResponseHead: owns one
incident's lifecycle end-to-end (spec §4: "receive incident_candidate
... maintain incident timeline ... close incident").

Implementation note (mirrors services/monitoring/head/__init__.py's
own note about its conceptual-diagram deviation): the spec's hierarchy
diagram doesn't show the Head doing work of its own beyond
orchestration, but exactly like MonitoringHead computes the composite
health score itself (rather than delegating that math to a lead),
IncidentResponseHead is the only place the final IncidentStatus is
*persisted* and *published* — the decision itself is already made by
Reporting Worker (services/incident_response/workers/reporting.py,
via utils.final_status_for) by the time this runs, since Recovery Lead
has already completed. See services/incident_response/workflows/
incident_response_graph.py for the exact node wiring: this is invoked
once, in the graph's final `finalize` node, after Communication Lead.

Deviation from a literal reading of services/monitoring/head/__init__.py's
own precedent: that file defines a `publish_phase_completed` instance
method the graph's publish_node never actually calls (verified against
the M3.7 codebase — `monitoring.phase.completed` is therefore never
published today; flagged as a pre-existing issue in
docs/M3.8_Incident_Response_Handover.md rather than silently fixed in
Monitoring's own file, per the handover's "existing services MUST NOT
be modified" rule). M3.8 does not repeat that mistake: `incident.
phase.completed` is queued directly in this execute()'s own
`nats_events`, so BaseAgent._post_execute's normal flush (which every
other event here already relies on) actually publishes it — no
side-channel method call from the graph required.
"""
from __future__ import annotations

from typing import Any, Dict, List

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.incident_response.integration.incident_repository import (
    IncidentRepository, IncidentTimelineRepository,
)
from services.incident_response.models import IncidentTimeline
from services.incident_response.utils import build_timeline_entry

log = structlog.get_logger(__name__)


@AgentFactory.register("incident_response_head")
class IncidentResponseHead(BaseAgent):
    """L3 — Sole orchestrator of one incident's lifecycle."""

    async def execute(self, task: TaskInput) -> AgentResult:
        arts = task.context.approved_artifacts
        incident_id = arts.get("__incident_id__")
        component = arts.get("__component__")
        severity = arts.get("__severity__")
        breach_cycles = int(arts.get("__breach_cycles__", 1))
        project_id = arts.get("incident_classifier_worker", {}).get("correlated_project_id")

        classification = arts.get("incident_classifier_worker", {}).get("classification", {})
        recovery_output = arts.get("recovery_worker", {})
        final_status = arts.get("__final_status__", "monitoring")

        try:
            async with self._db_factory() as db:
                await IncidentRepository.get_or_create(
                    db, incident_id, component, severity, breach_cycles, project_id=project_id)
                await IncidentTimelineRepository.record(
                    db, incident_id, "incident_opened",
                    f"Incident opened for {component} ({severity}), breach_cycles={breach_cycles}.")
                await IncidentTimelineRepository.record(
                    db, incident_id, "incident_classified",
                    classification.get("rationale", "Classified."))
                if recovery_output:
                    await IncidentTimelineRepository.record(
                        db, incident_id, "recovery_action",
                        f"Recovery status: {recovery_output.get('recovery_status', 'skipped')}.")
                await IncidentTimelineRepository.record(
                    db, incident_id, f"incident_{final_status}",
                    f"Incident marked {final_status}.")
                await IncidentRepository.update_status(db, incident_id, final_status)
                rows = await IncidentTimelineRepository.list_for(db, incident_id)
        except Exception as e:
            log.warning("incident_response_head_persist_failed", error=str(e))
            rows = []

        timeline_entries = [
            build_timeline_entry(r.event_type, r.message, r.actor, r.occurred_at)
            for r in rows
        ] if rows else [build_timeline_entry("incident_opened", f"Incident opened for {component}.")]
        timeline = IncidentTimeline(incident_id=incident_id, entries=timeline_entries)
        timeline_artifact = await self.create_artifact(task, "incident_timeline", timeline.model_dump(mode="json"))

        nats_events: List[NATSEvent] = [
            NATSEvent(subject="incident.updated", payload={
                "incident_id": incident_id, "status": final_status, "component": component,
            }),
            NATSEvent(subject="incident.phase.completed", payload={
                "incident_id": incident_id, "status": final_status,
            }),
        ]
        if final_status in ("resolved", "closed"):
            nats_events.append(NATSEvent(subject="incident.resolved", payload={
                "incident_id": incident_id, "component": component, "status": final_status,
            }))

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"incident_id": incident_id, "status": final_status, "component": component},
            summary=f"Incident {incident_id} finalized: status={final_status}",
            quality_score=1.0, artifacts=[timeline_artifact], nats_events=nats_events,
        )
