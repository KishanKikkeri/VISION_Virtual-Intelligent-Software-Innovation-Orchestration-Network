"""
infrastructure/database/workflow_designer_models.py
=================================
M4.8 §12 Repository — four new, additive-only tables, named exactly per
the brief:

    workflow_layouts     — one row per distinct workflow name: its
                           current `WorkflowLayout` document (nodes/edges/
                           groups/viewport/metadata as JSON) plus version
                           string. A workflow's *current* layout is its
                           `workflow_layouts` row; every prior state lives
                           in `canvas_snapshots` (append-only history,
                           same "current row + history table" split
                           M4.7's `Plugin`/`PluginInstallation` pair
                           established).
    designer_sessions     — one row per open editing session (brief's
                           `DesignerSession`).
    canvas_snapshots       — one row per durable point-in-time layout
                           capture (brief's `CanvasSnapshot`) — save/
                           autosave/pre-import/version-restore.
    node_templates         — one row per custom node template a user has
                           saved for reuse (brief's `NodeTemplate`);
                           builtin templates are not rows here (see
                           `node_library.BUILTIN_TEMPLATES` — they are
                           code, not data, so they need no migration to
                           add one).

**Scope note — identical situation to every prior M4.x DB models file in
this slice:** this milestone was written against a sandbox slice that
does not include the platform's real `infrastructure/database/models.py`.
This module tries the real `Base` first, falling back to a local one
only for this milestone's own tests. Before this ships: merge these four
class bodies into the real `models.py` (or confirm the import resolves)
— see `docs/M4.8_Workflow_Designer_Handover.md` §3.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import relationship

try:
    from infrastructure.database.models import Base  # the real declarative base, once available
except Exception:  # noqa: BLE001 — see module docstring
    from sqlalchemy.orm import declarative_base
    Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class WorkflowLayoutRow(Base):
    """One row per distinct workflow name — its current layout
    document."""
    __tablename__ = "workflow_layouts"

    workflow_name = Column(String, primary_key=True)
    version = Column(String, nullable=False, default="0.0.1")
    layout_json = Column(JSON, nullable=False)  # full WorkflowLayout.model_dump(mode="json")
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

    snapshots = relationship("CanvasSnapshotRow", back_populates="workflow", cascade="all, delete-orphan")
    sessions = relationship("DesignerSessionRow", back_populates="workflow", cascade="all, delete-orphan")


class DesignerSessionRow(Base):
    """One open editing session against one workflow."""
    __tablename__ = "designer_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    workflow_name = Column(String, ForeignKey("workflow_layouts.workflow_name"), nullable=False, index=True)
    user_id = Column(String, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    last_activity_at = Column(DateTime, default=datetime.utcnow, index=True)
    dirty = Column(Boolean, nullable=False, default=False)
    replay_mode = Column(Boolean, nullable=False, default=False)

    workflow = relationship("WorkflowLayoutRow", back_populates="sessions")


class CanvasSnapshotRow(Base):
    """One durable, named point-in-time layout capture."""
    __tablename__ = "canvas_snapshots"

    id = Column(String, primary_key=True, default=_uuid)
    workflow_name = Column(String, ForeignKey("workflow_layouts.workflow_name"), nullable=False, index=True)
    layout_json = Column(JSON, nullable=False)
    reason = Column(String, nullable=False, default="save")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    workflow = relationship("WorkflowLayoutRow", back_populates="snapshots")


class NodeTemplateRow(Base):
    """One saved custom node template (see module docstring — builtin
    templates are not persisted rows)."""
    __tablename__ = "node_templates"

    id = Column(String, primary_key=True, default=_uuid)
    node_type = Column(String, nullable=False, default="custom")
    category = Column(String, nullable=False, default="custom")
    label = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String, nullable=True)
    default_config = Column(JSON, nullable=True)
    property_schema = Column(JSON, nullable=True)
    source = Column(String, nullable=False, default="custom")
    created_at = Column(DateTime, default=datetime.utcnow)
