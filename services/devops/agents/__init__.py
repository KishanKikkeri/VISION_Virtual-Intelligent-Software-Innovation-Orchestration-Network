"""
services/devops/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.devops.agents" and looks
up a class by converting agent_id -> PascalCase (e.g. "devops_head" ->
"DevopsHead"). The actual M3.6 hierarchical implementation lives in
workers/, leads/, and head/ (mirroring services/qa and services/security).
Every concrete class here is registered directly via the
@AgentFactory.register(...) decorator at import time (see
core/runtime/factory.py's _class_registry), so exact PascalCase
capitalization of the class NAME itself is cosmetic — lookup by
agent_id string always hits the decorator-populated registry first.
This module just re-exports every concrete class for discoverability.
"""
from __future__ import annotations

from services.devops.head import DevOpsHead
from services.devops.leads import CicdLead, ContainerLead, InfrastructureOpsLead
from services.devops.workers import (
    DockerComposeWorker,
    DockerfileWriterWorker,
    EnvironmentConfigWorker,
    HealthCheckWorker,
    PipelineConfigWorker,
    ProvisionerWorker,
)

__all__ = [
    "DevOpsHead",
    "ContainerLead", "CicdLead", "InfrastructureOpsLead",
    "DockerfileWriterWorker", "DockerComposeWorker",
    "PipelineConfigWorker", "EnvironmentConfigWorker",
    "ProvisionerWorker", "HealthCheckWorker",
]
