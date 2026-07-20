"""
services/integration/plugin_sdk/plugin_repository.py
=================================
M4.7 "Reuse: repository patterns" — every other module in this
package only ever imports `PluginRepository` (never
`infrastructure.database.plugin_sdk_models` directly), same convention
M4.5's `chaos_repository.py`/M4.6's `security_repository.py` both set
for their own callers.

`record_plugin` upserts the `Plugin` catalog row (a plugin's manifest
fields rarely change after install — a version bump is a reinstall,
handled by upserting the same id) and always appends a new
`PluginInstallation` row (append-only state history — see
`plugin_sdk_models.py`'s module docstring for why).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from services.integration.plugin_sdk.plugin_models import (
    HookExecutionResult, HookType, PluginDependency, PluginExecutionResult, PluginManifest, PluginRecord,
    PluginSourceType, PluginState,
)


class PluginRepository:

    @staticmethod
    async def record_plugin(
        db: Any, manifest: PluginManifest, source_type: PluginSourceType, source_path: Optional[str] = None,
        state: PluginState = PluginState.INSTALLED,
    ) -> str:
        """Upserts the `Plugin` catalog row and appends one
        `PluginInstallation` history row. Returns the plugin id
        (already known — `Plugin.id` is the manifest's own id, not a
        generated uuid — but returned for symmetry with every other
        M4.x repository's `record_*` returning the persisted row's
        identifier)."""
        from infrastructure.database.plugin_sdk_models import Plugin, PluginInstallation

        result = await db.execute(select(Plugin).where(Plugin.id == manifest.id))
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(Plugin(
                id=manifest.id, name=manifest.name, version=manifest.version, author=manifest.author,
                description=manifest.description, entrypoint=manifest.entrypoint, api_version=manifest.api_version,
                permissions=list(manifest.permissions), hooks=list(manifest.hooks),
                dependencies=[d.model_dump(mode="json") for d in manifest.dependencies],
            ))
        else:
            existing.name = manifest.name
            existing.version = manifest.version
            existing.author = manifest.author
            existing.description = manifest.description
            existing.entrypoint = manifest.entrypoint
            existing.api_version = manifest.api_version
            existing.permissions = list(manifest.permissions)
            existing.hooks = list(manifest.hooks)
            existing.dependencies = [d.model_dump(mode="json") for d in manifest.dependencies]
        await db.flush()

        db.add(PluginInstallation(
            plugin_id=manifest.id, state=state.value, source_type=source_type.value, source_path=source_path,
            enabled_at=datetime.utcnow() if state == PluginState.ENABLED else None,
        ))
        await db.flush()
        return manifest.id

    @staticmethod
    async def set_state(db: Any, plugin_id: str, state: PluginState, error: Optional[str] = None) -> None:
        """Appends a new `PluginInstallation` row reflecting a state
        change (enable/disable/error) — reuses the most recent row's
        `source_type`/`source_path` (a state change doesn't alter
        where the plugin came from)."""
        from infrastructure.database.plugin_sdk_models import PluginInstallation

        result = await db.execute(
            select(PluginInstallation).where(PluginInstallation.plugin_id == plugin_id)
            .order_by(PluginInstallation.changed_at.desc()).limit(1)
        )
        latest = result.scalar_one_or_none()
        db.add(PluginInstallation(
            plugin_id=plugin_id, state=state.value,
            source_type=latest.source_type if latest else "python_package",
            source_path=latest.source_path if latest else None,
            enabled_at=datetime.utcnow() if state == PluginState.ENABLED else None,
            disabled_at=datetime.utcnow() if state == PluginState.DISABLED else None,
            last_error=error,
        ))
        await db.flush()

    @staticmethod
    async def get_plugin(db: Any, plugin_id: str) -> Optional[PluginRecord]:
        from infrastructure.database.plugin_sdk_models import Plugin, PluginInstallation

        result = await db.execute(select(Plugin).where(Plugin.id == plugin_id))
        plugin_row = result.scalar_one_or_none()
        if plugin_row is None:
            return None

        installation_result = await db.execute(
            select(PluginInstallation).where(PluginInstallation.plugin_id == plugin_id)
            .order_by(PluginInstallation.changed_at.desc()).limit(1)
        )
        installation_row = installation_result.scalar_one_or_none()
        return _to_record(plugin_row, installation_row)

    @staticmethod
    async def list_plugins(db: Any) -> List[PluginRecord]:
        from infrastructure.database.plugin_sdk_models import Plugin, PluginInstallation

        result = await db.execute(select(Plugin).order_by(Plugin.id))
        records: List[PluginRecord] = []
        for plugin_row in result.scalars().all():
            installation_result = await db.execute(
                select(PluginInstallation).where(PluginInstallation.plugin_id == plugin_row.id)
                .order_by(PluginInstallation.changed_at.desc()).limit(1)
            )
            installation_row = installation_result.scalar_one_or_none()
            records.append(_to_record(plugin_row, installation_row))
        return records

    @staticmethod
    async def record_execution(db: Any, execution: PluginExecutionResult) -> str:
        from infrastructure.database.plugin_sdk_models import PluginExecution

        row = PluginExecution(
            plugin_id=execution.plugin_id, action=execution.action, success=execution.success,
            duration_ms=execution.duration_ms, error=execution.error,
            executed_at=datetime.fromisoformat(execution.executed_at) if execution.executed_at else datetime.utcnow(),
            metadata_=execution.metadata,
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def record_hook_execution(db: Any, result: HookExecutionResult) -> str:
        from infrastructure.database.plugin_sdk_models import PluginHookExecution

        row = PluginHookExecution(
            plugin_id=result.plugin_id, hook=result.hook.value, success=result.success,
            duration_ms=result.duration_ms, error=result.error,
            executed_at=datetime.fromisoformat(result.executed_at) if result.executed_at else datetime.utcnow(),
        )
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_executions(db: Any, plugin_id: str, limit: int = 100) -> List[PluginExecutionResult]:
        from infrastructure.database.plugin_sdk_models import PluginExecution

        result = await db.execute(
            select(PluginExecution).where(PluginExecution.plugin_id == plugin_id)
            .order_by(PluginExecution.executed_at.desc()).limit(limit)
        )
        return [PluginExecutionResult(
            plugin_id=r.plugin_id, action=r.action, success=r.success, duration_ms=r.duration_ms or 0.0,
            error=r.error, executed_at=r.executed_at.isoformat() if r.executed_at else "", metadata=r.metadata_ or {},
        ) for r in result.scalars().all()]

    @staticmethod
    async def list_hook_executions(db: Any, plugin_id: str, limit: int = 100) -> List[HookExecutionResult]:
        from infrastructure.database.plugin_sdk_models import PluginHookExecution

        result = await db.execute(
            select(PluginHookExecution).where(PluginHookExecution.plugin_id == plugin_id)
            .order_by(PluginHookExecution.executed_at.desc()).limit(limit)
        )
        return [HookExecutionResult(
            plugin_id=r.plugin_id, hook=HookType(r.hook), success=r.success, duration_ms=r.duration_ms or 0.0,
            error=r.error, executed_at=r.executed_at.isoformat() if r.executed_at else "",
        ) for r in result.scalars().all()]


def _to_record(plugin_row: Any, installation_row: Optional[Any]) -> PluginRecord:
    manifest = PluginManifest(
        id=plugin_row.id, name=plugin_row.name, version=plugin_row.version, author=plugin_row.author,
        description=plugin_row.description or "", entrypoint=plugin_row.entrypoint,
        api_version=plugin_row.api_version, permissions=plugin_row.permissions or [],
        hooks=plugin_row.hooks or [],
        dependencies=[PluginDependency(**d) for d in (plugin_row.dependencies or [])],
    )
    if installation_row is None:
        return PluginRecord(manifest=manifest, state=PluginState.INSTALLED, installed_at="")
    return PluginRecord(
        manifest=manifest, state=PluginState(installation_row.state),
        source_type=PluginSourceType(installation_row.source_type), source_path=installation_row.source_path,
        installed_at=installation_row.changed_at.isoformat() if installation_row.changed_at else "",
        enabled_at=installation_row.enabled_at.isoformat() if installation_row.enabled_at else None,
        disabled_at=installation_row.disabled_at.isoformat() if installation_row.disabled_at else None,
        last_error=installation_row.last_error,
    )


async def fetch_plugin_dashboard_section(db: Any = None) -> Optional[Dict[str, Any]]:
    """M4.7's Dashboard Integration analog to M4.5/M4.6's own
    `fetch_*_dashboard_section` functions — the one function
    `services/integration/dashboard/dashboard_builder.py`'s
    `build_plugins_summary` consumes. Returns `None` only if the
    repository call itself raises (plugin tables/DB not wired); an
    empty plugin list is a normal "no plugins installed yet" payload,
    not `None` (same distinction every prior dashboard-section fetch
    function draws)."""
    if db is None:
        return {"installed_count": 0, "enabled_count": 0, "disabled_count": 0, "error_count": 0, "unhealthy_plugins": []}

    import structlog
    log = structlog.get_logger(__name__)
    try:
        records = await PluginRepository.list_plugins(db)
    except Exception as e:  # noqa: BLE001 — degrades this card, not the whole dashboard
        log.info("plugin_dashboard_history_unavailable", error=str(e))
        return None

    from services.integration.plugin_sdk.plugin_runtime import compute_health

    unhealthy: List[str] = []
    for record in records:
        try:
            hook_executions = await PluginRepository.list_hook_executions(db, record.manifest.id, limit=50)
        except Exception:  # noqa: BLE001
            hook_executions = []
        health = compute_health(record.manifest.id, record.state, hook_executions)
        if not health.healthy:
            unhealthy.append(record.manifest.id)

    return {
        "installed_count": len(records),
        "enabled_count": sum(1 for r in records if r.state == PluginState.ENABLED),
        "disabled_count": sum(1 for r in records if r.state == PluginState.DISABLED),
        "error_count": sum(1 for r in records if r.state == PluginState.ERROR),
        "unhealthy_plugins": unhealthy,
    }
