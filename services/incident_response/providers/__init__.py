"""
services/incident_response/providers — provider abstraction + implementations
for correlated evidence gathering and recovery/notification side effects.

Mirrors services/monitoring/providers's shape: this module exposes
`evidence_providers()`, which builds one instance of every read-only
evidence provider from the injected infra clients a worker already has
access to via BaseAgent (self._db_factory).
"""
from __future__ import annotations

from typing import Any, List, Optional

from services.incident_response.providers.base import EvidenceProvider
from services.incident_response.providers.devops_provider import DevOpsProvider
from services.incident_response.providers.monitoring_provider import MonitoringProvider
from services.incident_response.providers.notification_provider import NotificationProvider
from services.incident_response.providers.repository_provider import RepositoryProvider
from services.incident_response.providers.websocket_provider import IncidentWebSocketProvider

__all__ = [
    "EvidenceProvider",
    "MonitoringProvider", "RepositoryProvider", "DevOpsProvider",
    "NotificationProvider", "IncidentWebSocketProvider",
    "evidence_providers",
]


def evidence_providers(db_factory: Any, devops_base_url: Optional[str] = None) -> List[EvidenceProvider]:
    """The 3 read-only correlation providers evidence_collection_worker consults."""
    return [
        MonitoringProvider(db_factory),
        RepositoryProvider(db_factory),
        DevOpsProvider(db_factory, devops_base_url),
    ]
