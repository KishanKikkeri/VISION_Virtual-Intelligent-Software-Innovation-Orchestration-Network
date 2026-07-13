"""
services/monitoring/providers — provider abstraction + implementations
for all 9 monitored components (spec §6).

Mirrors services/devops/providers's `default_provider()` shape: this
module exposes `infrastructure_providers()` / `application_providers()`,
which build one instance of every provider from the injected infra
clients a worker already has access to via BaseAgent
(self._db_factory / self._nats / self._qdrant).
"""
from __future__ import annotations

from typing import Any, List

from services.monitoring.providers.base import MetricsProvider, MetricsProviderProtocol
from services.monitoring.providers.deployment_provider import DeploymentProvider
from services.monitoring.providers.docker_provider import DockerProvider
from services.monitoring.providers.nats_provider import NatsProvider
from services.monitoring.providers.postgres_provider import PostgresProvider
from services.monitoring.providers.qdrant_provider import QdrantProvider
from services.monitoring.providers.repository_provider import RepositoryProvider
from services.monitoring.providers.telemetry_provider import (
    AgentRuntimeTelemetryProvider,
    LLMProvidersTelemetryProvider,
    WebSocketTelemetryProvider,
)

__all__ = [
    "MetricsProvider", "MetricsProviderProtocol",
    "PostgresProvider", "QdrantProvider", "NatsProvider", "DockerProvider",
    "WebSocketTelemetryProvider", "LLMProvidersTelemetryProvider", "AgentRuntimeTelemetryProvider",
    "RepositoryProvider", "DeploymentProvider",
    "infrastructure_providers", "application_providers",
]


def infrastructure_providers(db_factory: Any, qdrant_client: Any, nats_client: Any) -> List[MetricsProvider]:
    """The 4 providers Infrastructure Metrics Worker collects (spec §1)."""
    return [
        PostgresProvider(db_factory),
        QdrantProvider(qdrant_client),
        NatsProvider(nats_client),
        DockerProvider(),
    ]


def application_providers(db_factory: Any) -> List[MetricsProvider]:
    """The 5 providers Application Metrics Worker collects (spec §1)."""
    return [
        WebSocketTelemetryProvider(),
        LLMProvidersTelemetryProvider(),
        AgentRuntimeTelemetryProvider(),
        RepositoryProvider(db_factory),
        DeploymentProvider(db_factory),
    ]
