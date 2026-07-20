"""
services/integration/api/designer.py
=================================
M4.8 §13 API section:

    GET    /designer/workflows
    GET    /designer/workflows/{id}
    POST   /designer/workflows
    PUT    /designer/workflows/{id}
    DELETE /designer/workflows/{id}
    GET    /designer/library
    GET    /designer/history/{id}
    POST   /designer/validate
    POST   /designer/export
    POST   /designer/import
    POST   /designer/replay/{project}
    GET    /designer/plugins

Own `APIRouter`, same "new endpoints, no rewrites" convention M4.7's
`api/plugins.py` established.

**Persistence**: this router keeps an in-memory `Dict[str, WorkflowLayout]`
per FastAPI app (`request.app.state.designer_workflows`, process-local)
when `request.app.state.db_factory` is not configured — same DB-preferred-
with-in-memory-fallback pattern `api/plugins.py` uses for its registry.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Request

from services.integration.workflow_designer import designer_export, node_library, validation_bridge
from services.integration.workflow_designer.designer_models import WorkflowLayout
from services.integration.workflow_designer.workflow_deserializer import import_mermaid, import_workflow
from services.integration.workflow_designer.workflow_serializer import SerializerError

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/designer", tags=["Workflow Designer"])


def _memory_store(request: Request) -> Dict[str, WorkflowLayout]:
    if not hasattr(request.app.state, "designer_workflows"):
        request.app.state.designer_workflows = {}
    return request.app.state.designer_workflows


def _db_factory(request: Request) -> Optional[Any]:
    return getattr(request.app.state, "db_factory", None)


@router.get("/workflows")
async def list_workflows(request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.workflow_designer.designer_repository import DesignerRepository
            async with db_factory() as db:
                layouts = await DesignerRepository.list_layouts(db)
            return {"count": len(layouts), "workflows": [l.model_dump(mode="json") for l in layouts],
                    "source": "database"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read workflow layouts: {e}")

    store = _memory_store(request)
    layouts = sorted(store.values(), key=lambda l: l.workflow_name)
    return {"count": len(layouts), "workflows": [l.model_dump(mode="json") for l in layouts], "source": "in-memory"}


@router.get("/library")
async def get_library(request: Request) -> Dict[str, Any]:
    """§2 Node Library — includes plugin templates when a
    `plugin_registry`/loaded-modules map is available on app state (§10
    Plugin Integration); otherwise builtin templates only, no error."""
    loaded_modules = getattr(request.app.state, "plugin_modules", None)
    library = node_library.build_library(loaded_modules=loaded_modules)
    return library.model_dump(mode="json")


@router.get("/plugins")
async def get_designer_plugins(request: Request) -> Dict[str, Any]:
    """§10 — the designer-specific plugin extension surface (custom
    nodes/property editors/validation rules/toolbar+context menu
    actions), distinct from `GET /plugins` (M4.7's own general plugin
    inventory route)."""
    loaded_modules = getattr(request.app.state, "plugin_modules", None)
    templates = node_library.plugin_node_templates(loaded_modules)
    actions = node_library.plugin_actions(loaded_modules)
    return {"node_templates": [t.model_dump(mode="json") for t in templates],
            "actions": [a.model_dump(mode="json") for a in actions]}


@router.get("/workflows/{workflow_name}")
async def get_workflow(workflow_name: str, request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.workflow_designer.designer_repository import DesignerRepository
            async with db_factory() as db:
                layout = await DesignerRepository.get_layout(db, workflow_name)
            if layout is not None:
                return layout.model_dump(mode="json")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read workflow layout: {e}")

    store = _memory_store(request)
    layout = store.get(workflow_name)
    if layout is None:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_name!r} not found")
    return layout.model_dump(mode="json")


async def _persist_or_store(request: Request, layout: WorkflowLayout, reason: str = "save") -> bool:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.workflow_designer.designer_repository import DesignerRepository
            async with db_factory() as db:
                await DesignerRepository.save_layout(db, layout, reason=reason)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("designer_layout_persist_failed", workflow_name=layout.workflow_name, error=str(e))
            return False
    _memory_store(request)[layout.workflow_name] = layout
    return True


@router.post("/workflows")
async def create_workflow(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        layout = WorkflowLayout.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid workflow layout: {e}")

    persisted = await _persist_or_store(request, layout, reason="save")
    return {"persisted": persisted, "workflow": layout.model_dump(mode="json")}


@router.put("/workflows/{workflow_name}")
async def update_workflow(workflow_name: str, request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    body = {**body, "workflow_name": workflow_name}
    try:
        layout = WorkflowLayout.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid workflow layout: {e}")

    persisted = await _persist_or_store(request, layout, reason="save")
    return {"persisted": persisted, "workflow": layout.model_dump(mode="json")}


@router.delete("/workflows/{workflow_name}")
async def delete_workflow(workflow_name: str, request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.workflow_designer.designer_repository import DesignerRepository
            async with db_factory() as db:
                deleted = await DesignerRepository.delete_layout(db, workflow_name)
            if not deleted:
                raise HTTPException(status_code=404, detail=f"workflow {workflow_name!r} not found")
            return {"deleted": workflow_name}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not delete workflow layout: {e}")

    store = _memory_store(request)
    if workflow_name not in store:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_name!r} not found")
    del store[workflow_name]
    return {"deleted": workflow_name}


@router.get("/history/{workflow_name}")
async def get_history(workflow_name: str, request: Request) -> Dict[str, Any]:
    """§8 "Compare against previous versions" — returns `canvas_snapshots`
    history when a database is wired; `[]` (not an error) otherwise, same
    "absence is not an error" convention every M4.x optional-integration
    route uses."""
    db_factory = _db_factory(request)
    if db_factory is None:
        return {"workflow_name": workflow_name, "snapshots": []}
    try:
        from services.integration.workflow_designer.designer_repository import DesignerRepository
        async with db_factory() as db:
            snapshots = await DesignerRepository.list_snapshots(db, workflow_name)
        return {"workflow_name": workflow_name, "snapshots": [s.model_dump(mode="json") for s in snapshots]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"could not read snapshot history: {e}")


@router.post("/validate")
async def validate_workflow(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        layout = WorkflowLayout.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid workflow layout: {e}")
    result = validation_bridge.validate_layout(layout)
    return result.model_dump(mode="json")


@router.post("/export")
async def export_workflow_route(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    fmt = body.get("format", "json")
    layout_data = body.get("layout")
    if layout_data is None:
        raise HTTPException(status_code=422, detail="body must include 'layout'")
    try:
        layout = WorkflowLayout.model_validate(layout_data)
        content = designer_export.export_workflow(layout, fmt)
    except SerializerError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid workflow layout: {e}")
    return {"format": fmt, "content": content}


@router.post("/import")
async def import_workflow_route(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    fmt = body.get("format", "json")
    content = body.get("content")
    if not content:
        raise HTTPException(status_code=422, detail="body must include 'content'")
    try:
        layout = import_mermaid(content) if fmt == "mermaid" else import_workflow(content, fmt)
    except SerializerError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"workflow": layout.model_dump(mode="json")}


@router.post("/replay/{workflow_name}")
async def replay_overlay_route(workflow_name: str, request: Request,
                                body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    execution_id = body.get("execution_id")
    if not execution_id:
        raise HTTPException(status_code=422, detail="body must include 'execution_id'")
    db_factory = _db_factory(request)
    db = None
    if db_factory is not None:
        async with db_factory() as db_session:
            overlay = validation_bridge.fetch_replay_overlay(workflow_name, execution_id, db_session)
            return overlay.model_dump(mode="json")
    overlay = validation_bridge.fetch_replay_overlay(workflow_name, execution_id, db)
    return overlay.model_dump(mode="json")
