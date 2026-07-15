"""
services/incident_response/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.incident_response.agents"
when department == "incident_response" (see core/runtime/factory.py's
dept_map). The actual M3.8 hierarchical implementation lives in
workers/, leads/, and head/ (mirroring services/monitoring/agents
exactly). Every concrete class is registered directly via the
@AgentFactory.register(...) decorator at import time, so this module's
only job is making sure all ten classes are imported (triggering their
decorators) and re-exported for discoverability.
"""
from __future__ import annotations

from services.incident_response.head import IncidentResponseHead
from services.incident_response.leads import CommunicationLead, IncidentAnalysisLead, RecoveryLead
from services.incident_response.workers import (
    EvidenceCollectionWorker,
    IncidentClassifierWorker,
    NotificationWorker,
    RecoveryWorker,
    ReportingWorker,
    RollbackWorker,
)

__all__ = [
    "IncidentResponseHead",
    "IncidentAnalysisLead", "RecoveryLead", "CommunicationLead",
    "IncidentClassifierWorker", "EvidenceCollectionWorker",
    "RollbackWorker", "RecoveryWorker",
    "NotificationWorker", "ReportingWorker",
]
