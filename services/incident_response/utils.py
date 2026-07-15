"""services/incident_response/utils.py — shared deterministic helpers used
across Incident Response providers/workers/leads/head.

Severity/action classification is deterministic (no LLM call) — same
"deterministic X" pattern already used by QA's Reporting Lead (M3.4),
Security's Risk Lead (M3.5), DevOps's Deployment/Release Lead (M3.6),
and Monitoring's own health-score/alert-dedup math (M3.7,
services/monitoring/utils.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from services.incident_response.models import (
    DEFAULT_BREACH_CYCLES_FOR_ROLLBACK,
    IncidentClassification,
    IncidentTimelineEntry,
    RecoveryActionType,
)
from services.monitoring.models import AlertSeverity, MonitoredComponent

# Components where a rollback is a meaningful recovery action at all —
# e.g. rolling back "postgres" or "nats" (infrastructure, not application
# code) doesn't mean anything; DevOps only rolls back *deployments*.
ROLLBACK_ELIGIBLE_COMPONENTS = (
    MonitoredComponent.DEPLOYMENTS,
    MonitoredComponent.REPOSITORY,
    MonitoredComponent.AGENT_RUNTIME,
)


def classify_incident(
    component: MonitoredComponent,
    severity: AlertSeverity,
    breach_cycles: int,
    recent_deployment_correlated: bool = False,
    breach_cycles_for_rollback: int = DEFAULT_BREACH_CYCLES_FOR_ROLLBACK,
) -> IncidentClassification:
    """
    Deterministic decision rule (spec §4/§0 Decision — frozen here for
    reproducibility in unit tests):

      CRITICAL + rollback-eligible component + breach_cycles >= threshold
      + a recent deployment correlates with the incident window
        -> ROLLBACK (auto-triggerable, no human approval required —
           Monitoring already required `breach_cycles_for_rollback`
           consecutive CRITICAL cycles before emitting incident_candidate
           at all, which is itself the approval gate)

      CRITICAL, otherwise                          -> RESTART
      WARNING                                       -> MANUAL (operator judgment)
      INFO / anything else                          -> NONE
    """
    if severity == AlertSeverity.CRITICAL:
        if (
            component in ROLLBACK_ELIGIBLE_COMPONENTS
            and breach_cycles >= breach_cycles_for_rollback
            and recent_deployment_correlated
        ):
            return IncidentClassification(
                severity=severity, recommended_action=RecoveryActionType.ROLLBACK,
                requires_approval=False,
                rationale=(
                    f"{component.value} CRITICAL for {breach_cycles} consecutive cycle(s), "
                    f"correlated with a recent deployment — rollback is the safest recovery."
                ),
            )
        return IncidentClassification(
            severity=severity, recommended_action=RecoveryActionType.RESTART,
            requires_approval=False,
            rationale=f"{component.value} CRITICAL for {breach_cycles} consecutive cycle(s).",
        )

    if severity == AlertSeverity.WARNING:
        return IncidentClassification(
            severity=severity, recommended_action=RecoveryActionType.MANUAL,
            requires_approval=True,
            rationale=f"{component.value} WARNING — operator review recommended.",
        )

    return IncidentClassification(
        severity=severity, recommended_action=RecoveryActionType.NONE,
        requires_approval=False, rationale="Severity does not warrant recovery action.",
    )


def build_timeline_entry(event_type: str, message: str,
                          actor: str = "incident_response_head",
                          now: Optional[datetime] = None) -> IncidentTimelineEntry:
    return IncidentTimelineEntry(
        event_type=event_type, message=message, actor=actor,
        occurred_at=now or datetime.utcnow(),
    )


def final_status_for(recovery_status: str, classification_action: RecoveryActionType) -> str:
    """
    Deterministic terminal-status decision for Incident Response Head's
    finalize step (spec §4 "close incident"):

      action == NONE                          -> resolved (nothing to recover from)
      action != NONE and recovery completed    -> resolved
      action != NONE and recovery failed       -> monitoring (needs a human)
      action != NONE and recovery skipped      -> monitoring
    """
    if classification_action == RecoveryActionType.NONE:
        return "resolved"
    if recovery_status == "completed":
        return "resolved"
    return "monitoring"


def summarize_incident(component: MonitoredComponent, severity: AlertSeverity,
                        action: RecoveryActionType, status: str) -> str:
    return (
        f"{component.value} incident classified {severity.value.upper()}; "
        f"recovery action={action.value}; final status={status}."
    )
