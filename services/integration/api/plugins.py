"""
services/integration/api/plugins.py
=================================
M4.7 §10 API section:

    GET    /plugins
    GET    /plugins/{id}
    POST   /plugins/install
    POST   /plugins/enable
    POST   /plugins/disable
    POST   /plugins/reload
    DELETE /plugins/{id}
    GET    /plugins/hooks
    GET    /plugins/capabilities
    GET    /plugins/health

Own `APIRouter`, same "new endpoints, no rewrites" convention M4.5's
`api/chaos.py`/M4.6's `api/security.py` both established.

**Registry lifetime**: this router keeps one `PluginRegistry` per
FastAPI app (`request.app.state.plugin_registry`, created on first
use) — process-local, same "process-local now, DB-backed once wired"
convention every prior M4.x API router uses for its own in-memory
fallback. When `request.app.state.db_factory` is configured, install/
enable/disable/reload additionally persist via `PluginRepository`, and
`GET /plugins`/`GET /plugins/{id}` prefer the DB-backed view (same
DB-preferred-with-in-memory-fallback pattern `api/chaos.py`/
`api/security.py` use).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Request

from services.integration.plugin_sdk import plugin_loader, plugin_validator
from services.integration.plugin_sdk.plugin_models import PluginHealth, PluginState
from services.integration.plugin_sdk.plugin_registry import PluginNotFoundError, PluginRegistry
from services.integration.plugin_sdk.plugin_runtime import compute_health

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/plugins", tags=["Plugin SDK"])


def _registry(request: Request) -> PluginRegistry:
    if not hasattr(request.app.state, "plugin_registry"):
        request.app.state.plugin_registry = PluginRegistry()
    return request.app.state.plugin_registry


def _plugins_dir(request: Request) -> str:
    return getattr(request.app.state, "plugins_dir", "./plugins")


def _db_factory(request: Request) -> Optional[Any]:
    return getattr(request.app.state, "db_factory", None)


@router.get("")
async def list_plugins(request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.plugin_sdk.plugin_repository import PluginRepository
            async with db_factory() as db:
                records = await PluginRepository.list_plugins(db)
            return {"count": len(records), "plugins": [r.model_dump(mode="json") for r in records],
                    "source": "database"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read plugin registry: {e}")

    records = _registry(request).list_installed()
    return {"count": len(records), "plugins": [r.model_dump(mode="json") for r in records], "source": "in-memory"}


@router.get("/hooks")
async def list_hooks(request: Request) -> Dict[str, List[str]]:
    return _registry(request).list_hooks()


@router.get("/capabilities")
async def list_capabilities(request: Request) -> Dict[str, Any]:
    return {"capabilities": [c.model_dump(mode="json") for c in _registry(request).list_capabilities()]}


@router.get("/health")
async def plugin_health(request: Request) -> Dict[str, Any]:
    registry = _registry(request)
    db_factory = _db_factory(request)
    results: List[PluginHealth] = []
    for record in registry.list_installed():
        executions = []
        if db_factory is not None:
            try:
                from services.integration.plugin_sdk.plugin_repository import PluginRepository
                async with db_factory() as db:
                    executions = await PluginRepository.list_hook_executions(db, record.manifest.id)
            except Exception as e:  # noqa: BLE001
                log.warning("plugin_health_lookup_failed", plugin_id=record.manifest.id, error=str(e))
        results.append(compute_health(record.manifest.id, record.state, executions))
    return {"count": len(results), "health": [h.model_dump(mode="json") for h in results]}


@router.get("/{plugin_id}")
async def get_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.plugin_sdk.plugin_repository import PluginRepository
            async with db_factory() as db:
                record = await PluginRepository.get_plugin(db, plugin_id)
            if record is not None:
                return record.model_dump(mode="json")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read plugin registry: {e}")

    record = _registry(request).get(plugin_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} is not installed")
    return record.model_dump(mode="json")


@router.post("/install")
async def install_plugin(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    plugin_id = body.get("plugin_id")
    if not plugin_id:
        raise HTTPException(status_code=422, detail="body must include 'plugin_id'")

    discovered = plugin_loader.discover_plugins(_plugins_dir(request), editable_paths=body.get("editable_paths"))
    target = next((d for d in discovered if d.manifest.id == plugin_id), None)
    if target is None:
        raise HTTPException(status_code=404,
                             detail=f"no plugin {plugin_id!r} found under {_plugins_dir(request)!r}")

    validation = plugin_validator.validate_manifest(target.manifest)
    if not validation.valid:
        raise HTTPException(status_code=422, detail={"message": "manifest validation failed",
                                                       "issues": [i.model_dump(mode="json") for i in validation.issues]})

    registry = _registry(request)
    try:
        record = registry.install(target.manifest, target.source_type, target.source_path)
    except Exception as e:  # noqa: BLE001 — PluginAlreadyInstalledError
        raise HTTPException(status_code=409, detail=str(e))

    db_factory = _db_factory(request)
    persisted = False
    if db_factory is not None:
        try:
            from services.integration.plugin_sdk.plugin_repository import PluginRepository
            async with db_factory() as db:
                await PluginRepository.record_plugin(db, target.manifest, target.source_type, target.source_path,
                                                       PluginState.INSTALLED)
            persisted = True
        except Exception as e:  # noqa: BLE001
            log.warning("plugin_install_persist_failed", plugin_id=plugin_id, error=str(e))

    return {"persisted": persisted, "plugin": record.model_dump(mode="json")}


async def _toggle(request: Request, plugin_id: str, state: PluginState) -> Dict[str, Any]:
    registry = _registry(request)
    try:
        record = registry.enable(plugin_id) if state == PluginState.ENABLED else registry.disable(plugin_id)
    except PluginNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    db_factory = _db_factory(request)
    persisted = False
    if db_factory is not None:
        try:
            from services.integration.plugin_sdk.plugin_repository import PluginRepository
            async with db_factory() as db:
                await PluginRepository.set_state(db, plugin_id, state)
            persisted = True
        except Exception as e:  # noqa: BLE001
            log.warning("plugin_state_persist_failed", plugin_id=plugin_id, error=str(e))

    return {"persisted": persisted, "plugin": record.model_dump(mode="json")}


@router.post("/enable")
async def enable_plugin(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    plugin_id = body.get("plugin_id")
    if not plugin_id:
        raise HTTPException(status_code=422, detail="body must include 'plugin_id'")
    return await _toggle(request, plugin_id, PluginState.ENABLED)


@router.post("/disable")
async def disable_plugin(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    plugin_id = body.get("plugin_id")
    if not plugin_id:
        raise HTTPException(status_code=422, detail="body must include 'plugin_id'")
    return await _toggle(request, plugin_id, PluginState.DISABLED)


@router.post("/reload")
async def reload_plugin(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Re-discovers the plugin's manifest/entrypoint and re-verifies
    the entrypoint still imports cleanly — brief's "hot reload." A
    plugin whose entrypoint now fails to import moves to `ERROR` state
    (via `PluginRegistry.mark_error`) rather than raising — reloading a
    broken plugin must not crash the platform, same failure-isolation
    principle `plugin_runtime.dispatch_hook` follows for hook calls."""
    plugin_id = body.get("plugin_id")
    if not plugin_id:
        raise HTTPException(status_code=422, detail="body must include 'plugin_id'")

    registry = _registry(request)
    if registry.get(plugin_id) is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} is not installed")

    discovered = plugin_loader.discover_plugins(_plugins_dir(request))
    target = next((d for d in discovered if d.manifest.id == plugin_id), None)
    if target is None:
        raise HTTPException(status_code=404,
                             detail=f"plugin {plugin_id!r} is installed but no longer found on disk")

    error = plugin_loader.validate_entrypoint_importable(target)
    if error is not None:
        record = registry.mark_error(plugin_id, error)
        return {"reloaded": False, "plugin": record.model_dump(mode="json"), "error": error}

    record = registry.enable(plugin_id)
    return {"reloaded": True, "plugin": record.model_dump(mode="json")}


@router.delete("/{plugin_id}")
async def remove_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
    registry = _registry(request)
    try:
        registry.remove(plugin_id)
    except PluginNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"removed": plugin_id}
