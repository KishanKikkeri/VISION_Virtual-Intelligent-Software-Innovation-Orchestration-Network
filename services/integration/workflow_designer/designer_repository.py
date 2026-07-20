"""
services/integration/workflow_designer/designer_repository.py
=================================
M4.8 §12 Repository — every other module in this package only ever
imports `DesignerRepository` (never
`infrastructure.database.workflow_designer_models` directly), same
convention M4.7's `plugin_repository.py` set for its own callers.

`save_layout` upserts the `workflow_layouts` current-row and (unless
`snapshot=False`) also appends a `canvas_snapshots` history row — same
"current row + append-only history" split
`plugin_repository.record_plugin`/`PluginInstallation` established.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import select

from services.integration.workflow_designer.designer_models import (
    CanvasSnapshot, DesignerSession, NodeTemplate, WorkflowLayout,
)


class DesignerRepository:

    # ── Layouts ──────────────────────────────────────────────────────

    @staticmethod
    async def save_layout(db: Any, layout: WorkflowLayout, snapshot: bool = True,
                           reason: str = "save") -> str:
        from infrastructure.database.workflow_designer_models import CanvasSnapshotRow, WorkflowLayoutRow

        result = await db.execute(
            select(WorkflowLayoutRow).where(WorkflowLayoutRow.workflow_name == layout.workflow_name)
        )
        existing = result.scalar_one_or_none()
        payload = layout.model_dump(mode="json")
        if existing is None:
            db.add(WorkflowLayoutRow(workflow_name=layout.workflow_name, version=layout.version,
                                      layout_json=payload))
        else:
            existing.version = layout.version
            existing.layout_json = payload
            existing.updated_at = datetime.utcnow()
        await db.flush()

        if snapshot:
            db.add(CanvasSnapshotRow(workflow_name=layout.workflow_name, layout_json=payload, reason=reason))
            await db.flush()
        return layout.workflow_name

    @staticmethod
    async def get_layout(db: Any, workflow_name: str) -> Optional[WorkflowLayout]:
        from infrastructure.database.workflow_designer_models import WorkflowLayoutRow

        result = await db.execute(select(WorkflowLayoutRow).where(WorkflowLayoutRow.workflow_name == workflow_name))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return WorkflowLayout.model_validate(row.layout_json)

    @staticmethod
    async def list_layouts(db: Any) -> List[WorkflowLayout]:
        from infrastructure.database.workflow_designer_models import WorkflowLayoutRow

        result = await db.execute(select(WorkflowLayoutRow).order_by(WorkflowLayoutRow.workflow_name))
        return [WorkflowLayout.model_validate(row.layout_json) for row in result.scalars().all()]

    @staticmethod
    async def delete_layout(db: Any, workflow_name: str) -> bool:
        from infrastructure.database.workflow_designer_models import WorkflowLayoutRow

        result = await db.execute(select(WorkflowLayoutRow).where(WorkflowLayoutRow.workflow_name == workflow_name))
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        await db.flush()
        return True

    # ── Snapshots (§8 "Restore older layouts") ──────────────────────

    @staticmethod
    async def list_snapshots(db: Any, workflow_name: str, limit: int = 50) -> List[CanvasSnapshot]:
        from infrastructure.database.workflow_designer_models import CanvasSnapshotRow

        result = await db.execute(
            select(CanvasSnapshotRow).where(CanvasSnapshotRow.workflow_name == workflow_name)
            .order_by(CanvasSnapshotRow.created_at.desc()).limit(limit)
        )
        return [
            CanvasSnapshot(id=r.id, workflow_name=r.workflow_name, layout=WorkflowLayout.model_validate(r.layout_json),
                           reason=r.reason, created_at=r.created_at.isoformat() if r.created_at else "")
            for r in result.scalars().all()
        ]

    @staticmethod
    async def get_snapshot(db: Any, snapshot_id: str) -> Optional[CanvasSnapshot]:
        from infrastructure.database.workflow_designer_models import CanvasSnapshotRow

        result = await db.execute(select(CanvasSnapshotRow).where(CanvasSnapshotRow.id == snapshot_id))
        r = result.scalar_one_or_none()
        if r is None:
            return None
        return CanvasSnapshot(id=r.id, workflow_name=r.workflow_name, layout=WorkflowLayout.model_validate(r.layout_json),
                               reason=r.reason, created_at=r.created_at.isoformat() if r.created_at else "")

    @staticmethod
    async def restore_snapshot(db: Any, snapshot_id: str) -> Optional[WorkflowLayout]:
        """§8 "Restore older layouts" — copies a `CanvasSnapshot`'s
        layout back onto the workflow's current row (recorded as a new
        `reason="version_restore"` snapshot too, so the restore itself
        is not lossy of whatever was current before it)."""
        snapshot = await DesignerRepository.get_snapshot(db, snapshot_id)
        if snapshot is None:
            return None
        await DesignerRepository.save_layout(db, snapshot.layout, snapshot=True, reason="version_restore")
        return snapshot.layout

    # ── Sessions ─────────────────────────────────────────────────────

    @staticmethod
    async def open_session(db: Any, workflow_name: str, user_id: Optional[str] = None) -> DesignerSession:
        from infrastructure.database.workflow_designer_models import DesignerSessionRow

        row = DesignerSessionRow(workflow_name=workflow_name, user_id=user_id)
        db.add(row)
        await db.flush()
        return DesignerSession(id=row.id, workflow_name=workflow_name, user_id=user_id,
                                opened_at=row.opened_at.isoformat() if row.opened_at else "",
                                last_activity_at=row.last_activity_at.isoformat() if row.last_activity_at else "")

    @staticmethod
    async def touch_session(db: Any, session_id: str, dirty: Optional[bool] = None,
                             replay_mode: Optional[bool] = None) -> Optional[DesignerSession]:
        from infrastructure.database.workflow_designer_models import DesignerSessionRow

        result = await db.execute(select(DesignerSessionRow).where(DesignerSessionRow.id == session_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.last_activity_at = datetime.utcnow()
        if dirty is not None:
            row.dirty = dirty
        if replay_mode is not None:
            row.replay_mode = replay_mode
        await db.flush()
        return DesignerSession(id=row.id, workflow_name=row.workflow_name, user_id=row.user_id,
                                opened_at=row.opened_at.isoformat() if row.opened_at else "",
                                last_activity_at=row.last_activity_at.isoformat() if row.last_activity_at else "",
                                dirty=row.dirty, replay_mode=row.replay_mode)

    # ── Node templates (custom, §2/§12) ─────────────────────────────

    @staticmethod
    async def save_node_template(db: Any, template: NodeTemplate) -> str:
        from infrastructure.database.workflow_designer_models import NodeTemplateRow

        row = NodeTemplateRow(node_type=template.node_type.value, category=template.category, label=template.label,
                               description=template.description, icon=template.icon,
                               default_config=template.default_config, property_schema=template.property_schema,
                               source=template.source)
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_node_templates(db: Any) -> List[NodeTemplate]:
        from infrastructure.database.workflow_designer_models import NodeTemplateRow

        result = await db.execute(select(NodeTemplateRow).order_by(NodeTemplateRow.category, NodeTemplateRow.label))
        return [
            NodeTemplate(node_type=r.node_type, category=r.category, label=r.label, description=r.description or "",
                         icon=r.icon or "shapes", default_config=r.default_config or {},
                         property_schema=r.property_schema or {}, source=r.source)
            for r in result.scalars().all()
        ]


async def fetch_designer_dashboard_section(db: Any = None) -> Optional[dict]:
    """M4.8's Dashboard Integration analog to M4.7's own
    `fetch_plugin_dashboard_section` — the one function
    `services/integration/dashboard/dashboard_builder.py`'s
    `build_designer_summary` consumes."""
    if db is None:
        return {"workflow_count": 0, "recent_edits": [], "invalid_count": 0}

    import structlog
    log = structlog.get_logger(__name__)
    try:
        layouts = await DesignerRepository.list_layouts(db)
    except Exception as e:  # noqa: BLE001 — degrades this card, not the whole dashboard
        log.info("designer_dashboard_history_unavailable", error=str(e))
        return None

    from services.integration.workflow_designer.validation_bridge import validate_layout

    recent = sorted(layouts, key=lambda l: l.updated_at, reverse=True)[:10]
    invalid_count = sum(1 for l in layouts if not validate_layout(l).valid)
    return {
        "workflow_count": len(layouts),
        "recent_edits": [{"workflow_name": l.workflow_name, "updated_at": l.updated_at} for l in recent],
        "invalid_count": invalid_count,
    }
