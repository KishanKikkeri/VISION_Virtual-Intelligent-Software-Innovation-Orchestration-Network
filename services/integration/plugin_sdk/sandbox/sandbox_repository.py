"""
services/integration/plugin_sdk/sandbox/sandbox_repository.py
=================================
M5.1 §"Repository" — every other module in this package only ever
imports `SandboxRepository` (never `infrastructure.database.
plugin_sandbox_models` directly), matching every other repository in
this codebase's own convention (see `production_repository.py`'s
docstring for the same rule stated for its package).
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import select

from services.integration.plugin_sdk.sandbox.sandbox_models import (
    ExecutionPolicy, SandboxExecutionRecord, SandboxLimits, SandboxMetrics, SandboxPolicy, SandboxPolicyRecord,
    SandboxResult, SandboxViolation, SandboxViolationRecord, ViolationKind,
)


class SandboxRepository:

    # ── Executions ───────────────────────────────────────────────────

    @staticmethod
    async def save_execution(db: Any, plugin_id: str, hook: str, mode: ExecutionPolicy,
                              result: SandboxResult) -> str:
        from infrastructure.database.plugin_sandbox_models import SandboxExecution

        row = SandboxExecution(
            plugin_id=plugin_id, hook=hook, execution_mode=mode.value, success=result.success,
            timed_out=result.timed_out, crashed=result.crashed, duration_ms=result.duration_ms,
            error=result.error,
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_executions(db: Any, plugin_id: Optional[str] = None, limit: int = 50) -> List[SandboxExecutionRecord]:
        from infrastructure.database.plugin_sandbox_models import SandboxExecution

        query = select(SandboxExecution).order_by(SandboxExecution.created_at.desc()).limit(limit)
        if plugin_id is not None:
            query = query.where(SandboxExecution.plugin_id == plugin_id)
        result = await db.execute(query)
        return [
            SandboxExecutionRecord(
                id=None, plugin_id=row.plugin_id, hook=row.hook,
                execution_mode=ExecutionPolicy(row.execution_mode), success=row.success,
                timed_out=row.timed_out, crashed=row.crashed, duration_ms=row.duration_ms, error=row.error,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in result.scalars().all()
        ]

    # ── Policies ─────────────────────────────────────────────────────

    @staticmethod
    async def save_policy(db: Any, policy: SandboxPolicy) -> str:
        from infrastructure.database.plugin_sandbox_models import SandboxPolicyRow

        row = SandboxPolicyRow(
            plugin_id=policy.plugin_id, execution_mode=policy.execution_mode.value,
            allowed_permissions=[p.value for p in policy.allowed_permissions],
            limits=policy.limits.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def latest_policy(db: Any, plugin_id: str) -> Optional[SandboxPolicy]:
        from infrastructure.database.plugin_sandbox_models import SandboxPolicyRow

        result = await db.execute(
            select(SandboxPolicyRow).where(SandboxPolicyRow.plugin_id == plugin_id)
            .order_by(SandboxPolicyRow.created_at.desc()).limit(1))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        from services.integration.plugin_sdk.plugin_models import Permission
        return SandboxPolicy(
            plugin_id=row.plugin_id, execution_mode=ExecutionPolicy(row.execution_mode),
            allowed_permissions=[Permission(p) for p in (row.allowed_permissions or [])],
            limits=SandboxLimits.model_validate(row.limits or {}),
        )

    @staticmethod
    async def list_policies(db: Any) -> List[SandboxPolicyRecord]:
        from infrastructure.database.plugin_sandbox_models import SandboxPolicyRow

        result = await db.execute(select(SandboxPolicyRow).order_by(SandboxPolicyRow.created_at.desc()))
        records = []
        from services.integration.plugin_sdk.plugin_models import Permission
        for row in result.scalars().all():
            policy = SandboxPolicy(
                plugin_id=row.plugin_id, execution_mode=ExecutionPolicy(row.execution_mode),
                allowed_permissions=[Permission(p) for p in (row.allowed_permissions or [])],
                limits=SandboxLimits.model_validate(row.limits or {}),
            )
            records.append(SandboxPolicyRecord(id=None, plugin_id=row.plugin_id, policy=policy,
                                                created_at=row.created_at.isoformat() if row.created_at else ""))
        return records

    # ── Violations ───────────────────────────────────────────────────

    @staticmethod
    async def save_violation(db: Any, violation: SandboxViolation) -> str:
        from infrastructure.database.plugin_sandbox_models import SandboxViolationRow

        row = SandboxViolationRow(
            plugin_id=violation.plugin_id, kind=violation.kind.value, detail=violation.detail,
            permission=violation.permission.value if violation.permission else None,
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_violations(db: Any, plugin_id: Optional[str] = None, limit: int = 50) -> List[SandboxViolationRecord]:
        from infrastructure.database.plugin_sandbox_models import SandboxViolationRow

        query = select(SandboxViolationRow).order_by(SandboxViolationRow.created_at.desc()).limit(limit)
        if plugin_id is not None:
            query = query.where(SandboxViolationRow.plugin_id == plugin_id)
        result = await db.execute(query)
        return [
            SandboxViolationRecord(
                id=None, plugin_id=row.plugin_id, kind=ViolationKind(row.kind), detail=row.detail,
                permission=row.permission, created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in result.scalars().all()
        ]

    # ── Metrics ──────────────────────────────────────────────────────

    @staticmethod
    async def save_metrics_snapshot(db: Any, metrics: SandboxMetrics) -> str:
        from infrastructure.database.plugin_sandbox_models import SandboxMetricsRow

        row = SandboxMetricsRow(
            plugin_id=metrics.plugin_id, execution_count=metrics.execution_count,
            success_count=metrics.success_count, failure_count=metrics.failure_count,
            timeout_count=metrics.timeout_count, crash_count=metrics.crash_count,
            violation_count=metrics.violation_count, total_duration_ms=metrics.total_duration_ms,
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def latest_metrics(db: Any, plugin_id: str) -> Optional[SandboxMetrics]:
        from infrastructure.database.plugin_sandbox_models import SandboxMetricsRow

        result = await db.execute(
            select(SandboxMetricsRow).where(SandboxMetricsRow.plugin_id == plugin_id)
            .order_by(SandboxMetricsRow.created_at.desc()).limit(1))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return SandboxMetrics(
            plugin_id=row.plugin_id, execution_count=int(row.execution_count),
            success_count=int(row.success_count), failure_count=int(row.failure_count),
            timeout_count=int(row.timeout_count), crash_count=int(row.crash_count),
            violation_count=int(row.violation_count), total_duration_ms=row.total_duration_ms,
        )
