"""
services/integration/api/sandbox.py
=================================
M5.1 §"API" section:

    GET  /plugins/sandbox/status
    GET  /plugins/sandbox/history
    POST /plugins/sandbox/run
    GET  /plugins/sandbox/policies
    PUT  /plugins/sandbox/policies

Own `APIRouter`, nested under `plugins.py`'s own `/plugins` prefix
(this milestone's brief's example paths — `/platform/plugins/sandbox/
*` — assume the same outer `/platform` mount every M4.x router in this
slice already assumes; see `api/plugins.py`'s own docstring for that
convention, unchanged here). **Strictly additive**: no existing route
in `api/plugins.py` is modified, added-to, or removed.

**Registry/monitor lifetime**: one `SandboxMonitor` per FastAPI app
(`request.app.state.sandbox_monitor`), process-local — same
"process-local now, DB-backed once wired" convention every prior M4.x
router uses. `POST /run` and `GET /status` work with zero database
configured; `GET/PUT /policies` and `GET /history` degrade to a 503
when `db_factory` isn't configured, since those two are meaningless
without persistence (matching `sandbox_cli.py`'s identical split).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Query, Request

from services.integration.plugin_sdk.plugin_loader import discover_plugins, import_entrypoint
from services.integration.plugin_sdk.plugin_models import HookType, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext
from services.integration.plugin_sdk.sandbox import sandbox_monitor
from services.integration.plugin_sdk.sandbox.sandbox_models import ExecutionPolicy, SandboxLimits
from services.integration.plugin_sdk.sandbox.sandbox_policy import build_policy
from services.integration.plugin_sdk.sandbox.sandbox_repository import SandboxRepository
from services.integration.plugin_sdk.sandbox.sandbox_runtime import SandboxManager

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/plugins/sandbox", tags=["Plugin Sandboxing"])


def _monitor(request: Request) -> sandbox_monitor.SandboxMonitor:
    if not hasattr(request.app.state, "sandbox_monitor"):
        request.app.state.sandbox_monitor = sandbox_monitor.SandboxMonitor()
    return request.app.state.sandbox_monitor


def _plugins_dir(request: Request) -> str:
    return getattr(request.app.state, "plugins_dir", "./plugins")


def _db_factory(request: Request) -> Optional[Any]:
    return getattr(request.app.state, "db_factory", None)


@router.get("/status")
async def get_status(request: Request) -> Dict[str, Any]:
    """Always available, zero-DB-required — the in-process `SandboxMonitor`
    summary, same "cheapest possible endpoint" convention M4.10's
    `api/release.py:get_status` established."""
    monitor = _monitor(request)
    return monitor.dashboard_summary().model_dump(mode="json")


@router.post("/run")
async def post_run(request: Request, plugin_id: str = Body(...), hook: str = Body(...),
                    mode: str = Body("none"), kwargs: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    plugins_dir = _plugins_dir(request)
    discovered = discover_plugins(plugins_dir)
    target = next((d for d in discovered if d.manifest.id == plugin_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} not found under {plugins_dir!r}")

    try:
        hook_enum = HookType(hook)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown hook {hook!r}")

    try:
        execution_mode = ExecutionPolicy(mode)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown execution mode {mode!r}")

    module = import_entrypoint(target)
    record = PluginRecord(manifest=target.manifest, source_path=target.source_path)
    policy = build_policy(plugin_id, target.manifest.permissions, execution_mode)

    manager = SandboxManager(plugins_dir, monitor=_monitor(request))
    result = manager.execute(record, module, hook_enum, PluginContext(), policy, **kwargs)

    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            async with db_factory() as db:
                await SandboxRepository.save_execution(db, plugin_id, hook, execution_mode, result)
                for violation in result.violations:
                    await SandboxRepository.save_violation(db, violation)
        except Exception as e:  # noqa: BLE001 — persistence failure must not hide a real execution result
            log.warning("sandbox_execution_persist_failed", plugin_id=plugin_id, error=str(e))

    return result.model_dump(mode="json")


@router.get("/history")
async def get_history(request: Request, plugin_id: Optional[str] = Query(None),
                       limit: int = Query(50)) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is None:
        raise HTTPException(status_code=503, detail="no database configured — sandbox execution history requires "
                                                      "request.app.state.db_factory to be set")
    try:
        async with db_factory() as db:
            executions = await SandboxRepository.list_executions(db, plugin_id, limit)
            return {"executions": [e.model_dump(mode="json") for e in executions]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read sandbox history: {e}") from e


@router.get("/policies")
async def get_policies(request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is None:
        raise HTTPException(status_code=503, detail="no database configured — sandbox policies require "
                                                      "request.app.state.db_factory to be set")
    try:
        async with db_factory() as db:
            records = await SandboxRepository.list_policies(db)
            return {"policies": [r.model_dump(mode="json") for r in records]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read sandbox policies: {e}") from e


@router.put("/policies")
async def put_policy(request: Request, plugin_id: str = Body(...), execution_mode: str = Body("none"),
                      permissions: List[str] = Body(default_factory=list),
                      limits: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
    try:
        mode = ExecutionPolicy(execution_mode)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown execution mode {execution_mode!r}")

    sandbox_limits = SandboxLimits.model_validate(limits) if limits else SandboxLimits()
    policy = build_policy(plugin_id, permissions, mode, limits=sandbox_limits)

    db_factory = _db_factory(request)
    if db_factory is None:
        raise HTTPException(status_code=503, detail="no database configured — sandbox policies require "
                                                      "request.app.state.db_factory to be set")
    try:
        async with db_factory() as db:
            await SandboxRepository.save_policy(db, policy)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not save sandbox policy: {e}") from e

    return policy.model_dump(mode="json")
