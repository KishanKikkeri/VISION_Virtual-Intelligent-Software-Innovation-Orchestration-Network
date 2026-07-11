"""
services/engineering/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.engineering.agents" and
looks up a class by converting agent_id -> PascalCase (e.g.
"engineering_head" -> "EngineeringHead"). The actual M3.3 hierarchical
implementation lives in workers/, leads/, and head/ (Stage 1 layout).
This module just re-exports every concrete class so that lookup keeps
working unchanged.
"""
from __future__ import annotations

from services.engineering.head import EngineeringHead
from services.engineering.leads import BackendLead, FrontendLead, IntegrationLead, ReviewLead
from services.engineering.workers import (
    ApiImplementationWorker,
    AuthenticationWorker,
    BusinessLogicWorker,
    CodeReviewerWorker,
    CommitWorker,
    ComponentWorker,
    DatabaseLayerWorker,
    InternalIntegrationWorker,
    MessagingWorker,
    PageWorker,
    QualityWorker,
    RefactorWorker,
    RoutingWorker,
    StateManagementWorker,
    ThirdPartyIntegrationWorker,
)

# agent_id "code_review_lead" -> PascalCase "CodeReviewLead". The concrete
# class is named ReviewLead (per the spec's renamed L4 title); alias it so
# AgentFactory's naming convention still resolves it.
CodeReviewLead = ReviewLead

__all__ = [
    "EngineeringHead",
    "BackendLead", "FrontendLead", "IntegrationLead", "ReviewLead", "CodeReviewLead",
    "DatabaseLayerWorker", "AuthenticationWorker", "BusinessLogicWorker", "ApiImplementationWorker",
    "ComponentWorker", "PageWorker", "StateManagementWorker", "RoutingWorker",
    "InternalIntegrationWorker", "ThirdPartyIntegrationWorker", "MessagingWorker",
    "CodeReviewerWorker", "RefactorWorker", "QualityWorker", "CommitWorker",
]
