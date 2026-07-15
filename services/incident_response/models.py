"""
services/incident_response/models.py — M3.8 Incident Response Service core models.
================================================================
Flat module (not a package), following the M3.6/M3.7 precedent — see
docs/M3.8_Incident_Response_Handover.md §Architecture.

Incident Response consumes `incident_candidate` artifacts and the
`monitoring.incident` NATS event already published by M3.7 Monitoring
(services/monitoring/models.py's IncidentCandidate, services/monitoring/
head/__init__.py). It does not redefine those — MonitoredComponent and
AlertSeverity are the frozen vocabulary for "what broke" and "how bad
was it when Monitoring detected it"; this module adds the vocabulary
for "what we did about it".
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.monitoring.models import AlertSeverity, MonitoredComponent


# -- Enums --------------------------------------------------------

class IncidentStatus(str, Enum):
    OPEN          = "open"
    INVESTIGATING = "investigating"
    MITIGATING    = "mitigating"
    MONITORING    = "monitoring"
    RESOLVED      = "resolved"
    CLOSED        = "closed"


class RecoveryActionType(str, Enum):
    ROLLBACK = "rollback"
    RESTART  = "restart"
    MANUAL   = "manual"
    NONE     = "none"


class RecoveryActionStatus(str, Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED   = "completed"
    FAILED      = "failed"
    SKIPPED     = "skipped"


class EvidenceSource(str, Enum):
    MONITORING = "monitoring"
    DEVOPS     = "devops"
    REPOSITORY = "repository"


class IncidentTaskStatus(str, Enum):
    """Mirrors services.monitoring.models.MonitoringTaskStatus exactly."""
    PENDING       = "pending"
    RUNNING       = "running"
    COMPLETED     = "completed"
    FAILED        = "failed"
    ESCALATED     = "escalated"
    DEAD_LETTERED = "dead_lettered"


# Frozen — deterministic classification thresholds (mirrors monitoring's
# HEALTHY_THRESHOLD/WARNING_THRESHOLD precedent of named, reused constants).
DEFAULT_BREACH_CYCLES_FOR_ROLLBACK = 3


# -- Evidence & timeline (Incident Analysis Lead) ----------------------

class EvidenceItem(BaseModel):
    """One correlated fact gathered by evidence_collection_worker."""
    source:       EvidenceSource
    ref:          str                       # e.g. deployment_id, pull_request id, alert_id
    summary:      str
    collected_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentTimelineEntry(BaseModel):
    event_type:  str
    message:     str
    actor:       str = "incident_response_head"
    occurred_at: datetime = Field(default_factory=datetime.utcnow)


# -- Classification (Incident Classifier Worker) -----------------------

class IncidentClassification(BaseModel):
    """Output of incident_classifier_worker — the deterministic decision
    of how bad this is and what to do about it (see context.py's
    classify_incident / determine_recovery_action)."""
    severity:            AlertSeverity
    recommended_action:  RecoveryActionType
    requires_approval:   bool = False
    rationale:           str = ""


# -- Recovery (Recovery Lead) ------------------------------------------

class RecoveryPlan(BaseModel):
    """Artifact content for `recovery_plan`."""
    incident_id:    str
    action_type:    RecoveryActionType
    component:      MonitoredComponent
    steps:          List[str] = Field(default_factory=list)
    status:         RecoveryActionStatus = RecoveryActionStatus.PENDING
    triggered_at:   Optional[datetime] = None
    completed_at:   Optional[datetime] = None
    detail:         Dict[str, Any] = Field(default_factory=dict)


# -- Root cause / remediation (Reporting Worker) -----------------------

class RootCauseAnalysis(BaseModel):
    """Artifact content for `root_cause_analysis`."""
    incident_id:         str
    probable_cause:      str
    contributing_factors: List[str] = Field(default_factory=list)
    confidence:          float = Field(ge=0.0, le=1.0, default=0.5)


class RemediationPlan(BaseModel):
    """Artifact content for `remediation_plan`."""
    incident_id:        str
    recommendations:    List[str] = Field(default_factory=list)
    preventive_actions: List[str] = Field(default_factory=list)


# -- Final report (Reporting Worker / Incident Response Head) ----------

class IncidentReport(BaseModel):
    """Artifact content for `incident_report`. Also mirrors what's persisted
    to infrastructure/database/models.py's IncidentReportRecord."""
    incident_id:   str
    component:     MonitoredComponent
    severity:      AlertSeverity
    status:        IncidentStatus
    summary:       str
    timeline:      List[IncidentTimelineEntry] = Field(default_factory=list)
    root_cause:    Optional[RootCauseAnalysis] = None
    remediation:   Optional[RemediationPlan] = None
    recovery_plan: Optional[RecoveryPlan] = None
    generated_at:  datetime = Field(default_factory=datetime.utcnow)


class IncidentTimeline(BaseModel):
    """Artifact content for `incident_timeline` (kept separate from
    incident_report per spec §12 — timeline is updated incrementally
    across the lifecycle, the report is generated once at closure)."""
    incident_id: str
    entries:     List[IncidentTimelineEntry] = Field(default_factory=list)


# -- Incident record (Incident Response Head) --------------------------

class IncidentRecord(BaseModel):
    """In-memory/API shape for one incident's current state. Mirrors
    infrastructure/database/models.py's Incident row."""
    incident_id:    str
    component:      MonitoredComponent
    severity:       AlertSeverity
    status:         IncidentStatus = IncidentStatus.OPEN
    breach_cycles:  int = 0
    created_at:     datetime = Field(default_factory=datetime.utcnow)
    updated_at:     datetime = Field(default_factory=datetime.utcnow)
    resolved_at:    Optional[datetime] = None
    closed_at:      Optional[datetime] = None


# -- Task bookkeeping (mirrors MonitoringTask) --------------------------

class IncidentTask(BaseModel):
    task_id:         str = Field(default_factory=lambda: str(uuid.uuid4()))
    worker_agent_id: str
    description:     str = ""
    depends_on:      List[str] = Field(default_factory=list)
    status:          IncidentTaskStatus = IncidentTaskStatus.PENDING
    retry_count:     int = 0
    max_retries:     int = 3
    failure_reason:  Optional[str] = None

    def can_run(self, completed_task_ids: set) -> bool:
        return (
            self.status == IncidentTaskStatus.PENDING
            and all(dep in completed_task_ids for dep in self.depends_on)
        )
