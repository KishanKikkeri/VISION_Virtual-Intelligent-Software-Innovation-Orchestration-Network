"""
services/repository/managers/__init__.py
===========================================
Shared helpers used by every manager, plus RepositoryDeps — the single
dependency-injection container Repository Service is built around.

No manager talks to a provider SDK directly and no manager talks to
the database with raw SQL — everything goes through:
  - self.deps.provider        (BaseRepositoryProvider)
  - self.deps.db_factory()    (AsyncSession context manager)
  - self.deps.nats            (NATSClient | None)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog

log = structlog.get_logger(__name__)

PROTECTED_BRANCHES = ("main", "develop")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def slugify(text: str) -> str:
    """Lowercases, strips non-alphanumerics, collapses to hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "repo"


def validate_slug(slug: str, field_name: str = "slug") -> None:
    from services.repository.schemas import InvalidBranchNameError
    if not slug or not _SLUG_RE.match(slug):
        raise InvalidBranchNameError(
            f"Invalid {field_name} '{slug}': must be lowercase "
            f"alphanumeric segments separated by single hyphens"
        )


def validate_task_id(task_id: str, field_name: str = "task-id") -> None:
    from services.repository.schemas import InvalidBranchNameError
    if not task_id or not _TASK_ID_RE.match(task_id):
        raise InvalidBranchNameError(
            f"Invalid {field_name} '{task_id}': only letters, digits, "
            f"'.', '_', '-' are allowed"
        )


def build_branch_name(branch_type: str, task_id: Optional[str],
                      incident_id: Optional[str], slug: Optional[str]) -> str:
    """
    Enforces the locked naming policy:
      feature/<task-id>-<slug>
      fix/<task-id>-<slug>
      hotfix/<incident-id>
      integration/<feature-name>          (Appendix A, M3.3 — Engineering Lead only)
    """
    from services.repository.schemas import InvalidBranchNameError

    if branch_type in ("feature", "fix"):
        if not task_id or not slug:
            raise InvalidBranchNameError(
                f"branch_type='{branch_type}' requires both task_id and slug"
            )
        validate_task_id(task_id)
        validate_slug(slug)
        return f"{branch_type}/{task_id}-{slug}"

    if branch_type == "hotfix":
        if not incident_id:
            raise InvalidBranchNameError("branch_type='hotfix' requires incident_id")
        validate_task_id(incident_id, field_name="incident-id")
        return f"hotfix/{incident_id}"

    if branch_type == "integration":
        # slug carries the feature-name here; no task_id/incident_id needed.
        if not slug:
            raise InvalidBranchNameError("branch_type='integration' requires slug (feature-name)")
        validate_slug(slug, field_name="feature-name")
        return f"integration/{slug}"

    raise InvalidBranchNameError(f"Unsupported branch_type '{branch_type}'")


def assert_not_protected(branch_name: str, action: str) -> None:
    from services.repository.schemas import ProtectedBranchViolationError
    if branch_name in PROTECTED_BRANCHES:
        raise ProtectedBranchViolationError(
            f"Refusing to {action} protected branch '{branch_name}'"
        )


@dataclass
class RepositoryDeps:
    """Everything a manager needs, built once at service startup."""
    db_factory: Callable[[], Any]      # -> async context manager yielding AsyncSession
    provider:   Any                    # BaseRepositoryProvider
    nats:       Optional[Any] = None   # NATSClient
    default_owner: Optional[str] = None
