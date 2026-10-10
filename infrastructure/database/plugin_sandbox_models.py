"""
infrastructure/database/plugin_sandbox_models.py
=================================
M5.1 §"Repository" — four new, additive-only tables:

    sandbox_executions   — one row per hook dispatch through SandboxManager
                          (plugin_id/hook/execution_mode/success/timed_out/
                          crashed/duration_ms/error), the sandboxed-execution
                          analogue of M4.7's `plugin_hook_executions`.
    sandbox_policies      — one row per plugin's current SandboxPolicy
                          (execution_mode/allowed_permissions/limits as JSON) —
                          append-only history like `plugin_installations`,
                          not a single mutable row, so a policy change is
                          auditable.
    sandbox_violations    — one row per SandboxViolation (denied-permission
                          attempt, limit exceeded, timeout, crash, malformed
                          IPC payload).
    sandbox_metrics       — one row per plugin's point-in-time aggregate
                          counters (a periodic snapshot, not recomputed
                          from `sandbox_executions` on every read — same
                          "materialized snapshot" choice M4.9's
                          `environment_checks` table makes for the same
                          reason: cheap to query without re-aggregating).

**Scope note — identical situation to every prior M4.x DB models file
in this slice** (see `infrastructure/database/plugin_sdk_models.py`'s
own docstring for the original statement of this): tries the real
`infrastructure.database.models.Base` first, falls back to a local one
only for this milestone's own tests. Before this ships: merge these
four class bodies into the real `models.py` (or confirm the import
resolves).

FKs to `plugins.id` are present but, per this project's own
`DATABASE_REVIEW.md` DB3 finding (no `ondelete=` specified anywhere in
this slice's existing FKs), this migration follows the same convention
deliberately for consistency with `0011`/`0012` rather than introducing
a one-off `ondelete=` policy this milestone alone would have — that
inconsistency-avoidance is itself the safer choice per DB3's own
"latent, not exercised by any code path" conclusion, which still holds
here (no code path in this milestone deletes a `plugins` row either).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, String, Text

try:
    from infrastructure.database.models import Base  # the real declarative base, once available
except Exception:  # noqa: BLE001 — see module docstring
    try:
        from infrastructure.database.plugin_sdk_models import Base  # reuse M4.7's fallback Base if present
    except Exception:  # noqa: BLE001
        from sqlalchemy.orm import declarative_base
        Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class SandboxExecution(Base):
    """One row per hook dispatch through `SandboxManager.execute`."""
    __tablename__ = "sandbox_executions"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    hook = Column(String, nullable=False, index=True)
    execution_mode = Column(String, nullable=False, default="none")
    success = Column(Boolean, nullable=False, default=True)
    timed_out = Column(Boolean, nullable=False, default=False)
    crashed = Column(Boolean, nullable=False, default=False)
    duration_ms = Column(Float, nullable=False, default=0.0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class SandboxPolicyRow(Base):
    """One row per policy assignment/change for a plugin — append-only,
    matching `plugin_installations`'s own history-not-mutation
    convention (see module docstring)."""
    __tablename__ = "sandbox_policies"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    execution_mode = Column(String, nullable=False, default="none")
    allowed_permissions = Column(JSON, nullable=True)  # List[str]
    limits = Column(JSON, nullable=True)               # SandboxLimits.model_dump()
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class SandboxViolationRow(Base):
    """One row per `SandboxViolation` recorded during any execution."""
    __tablename__ = "sandbox_violations"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    kind = Column(String, nullable=False, index=True)
    detail = Column(Text, nullable=False)
    permission = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class SandboxMetricsRow(Base):
    """One point-in-time aggregate snapshot per plugin — see module
    docstring for why this isn't recomputed from `sandbox_executions`
    on every read."""
    __tablename__ = "sandbox_metrics"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    execution_count = Column(Float, nullable=False, default=0)
    success_count = Column(Float, nullable=False, default=0)
    failure_count = Column(Float, nullable=False, default=0)
    timeout_count = Column(Float, nullable=False, default=0)
    crash_count = Column(Float, nullable=False, default=0)
    violation_count = Column(Float, nullable=False, default=0)
    total_duration_ms = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
