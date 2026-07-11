"""services/engineering/workers — 15 L5 worker implementations for M3.3."""
from __future__ import annotations

# Backend (4)
from services.engineering.workers.backend import (
    ApiImplementationWorker,
    AuthenticationWorker,
    BusinessLogicWorker,
    DatabaseLayerWorker,
)

# Frontend (4) — require ui_blueprint
from services.engineering.workers.frontend import (
    ComponentWorker,
    PageWorker,
    RoutingWorker,
    StateManagementWorker,
)

# Integration (3)
from services.engineering.workers.integration import (
    InternalIntegrationWorker,
    MessagingWorker,
    ThirdPartyIntegrationWorker,
)

# Review (4) — mandatory gate
from services.engineering.workers.review import (
    CodeReviewerWorker,
    CommitWorker,
    QualityWorker,
    RefactorWorker,
)

__all__ = [
    "DatabaseLayerWorker", "AuthenticationWorker", "BusinessLogicWorker", "ApiImplementationWorker",
    "ComponentWorker", "PageWorker", "StateManagementWorker", "RoutingWorker",
    "InternalIntegrationWorker", "ThirdPartyIntegrationWorker", "MessagingWorker",
    "CodeReviewerWorker", "RefactorWorker", "QualityWorker", "CommitWorker",
]
