"""
services/repository/models/__init__.py
=========================================
Repository Service's ORM models are defined once, centrally, in
infrastructure/database/models.py — the same convention every other
AASC service follows (see Project, Artifact, AuditEvent, etc.). This
module simply re-exports them under the service's own namespace so
`from services.repository.models import Repository` reads naturally
from inside services/repository/**, without a second copy of the
table definitions ever existing.
"""
from __future__ import annotations

from infrastructure.database.models import (
    Branch,
    PullRequest,
    Repository,
    RepositoryEvent,
)

__all__ = ["Repository", "Branch", "PullRequest", "RepositoryEvent"]
