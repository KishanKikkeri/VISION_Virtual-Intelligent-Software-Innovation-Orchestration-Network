"""
infrastructure/database/plugin_sdk_models.py
=================================
M4.7 §12 Persistence — four new, additive-only tables, named exactly
per the brief:

    plugins                  — one row per distinct plugin id: the
                               manifest's own fields (name/version/
                               author/entrypoint/api_version/
                               permissions/dependencies/hooks), kept as
                               its own table (not only nested JSON on
                               an installation row) so a caller can
                               query "every installed plugin's declared
                               permissions" without joining through
                               installation history.
    plugin_installations      — one row per install/state-change event
                               for a plugin (state/source_type/
                               source_path/timestamps/last_error) — a
                               plugin's *current* state is its most
                               recent row by `changed_at`, so
                               reinstall/enable/disable history is
                               preserved rather than overwritten.
    plugin_executions         — one row per general plugin invocation
                               (brief's `PluginExecution`) — action/
                               success/duration/error/metadata.
    plugin_hook_executions    — one row per hook dispatch to a plugin
                               (brief's `PluginHookExecution`) — its
                               own table (not folded into
                               `plugin_executions`) because hook
                               dispatches are the platform's single
                               highest-volume plugin interaction and
                               benefit from their own `hook` index for
                               "every before_workflow execution across
                               every plugin"-style queries.

**Scope note — identical situation to every prior M4.x DB models file
in this slice:** this milestone was written against a sandbox slice
that does not include the platform's real
`infrastructure/database/models.py`. This module tries the real `Base`
first, falling back to a local one only for this milestone's own
tests. Before this ships: merge these four class bodies into the real
`models.py` (or confirm the import resolves) — see
`docs/M4.7_Plugin_SDK_Handover.md` §3.
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


class Plugin(Base):
    """One row per distinct plugin id — the manifest's own fields."""
    __tablename__ = "plugins"

    id = Column(String, primary_key=True)  # the manifest's own id, not a generated uuid
    name = Column(String, nullable=False)
    version = Column(String, nullable=False)
    author = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    entrypoint = Column(String, nullable=False)
    api_version = Column(String, nullable=False, default="1.0.0")
    permissions = Column(JSON, nullable=True)     # List[str]
    dependencies = Column(JSON, nullable=True)    # List[{"plugin_id": ..., "version_constraint": ...}]
    hooks = Column(JSON, nullable=True)           # List[str]
    created_at = Column(DateTime, default=datetime.utcnow)

    installations = relationship("PluginInstallation", back_populates="plugin", cascade="all, delete-orphan")
    executions = relationship("PluginExecution", back_populates="plugin", cascade="all, delete-orphan")
    hook_executions = relationship("PluginHookExecution", back_populates="plugin", cascade="all, delete-orphan")


class PluginInstallation(Base):
    """One install/state-change event for a plugin — see module
    docstring for why this is append-only history rather than a
    single mutable row."""
    __tablename__ = "plugin_installations"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    state = Column(String, nullable=False, default="installed")
    source_type = Column(String, nullable=False, default="python_package")
    source_path = Column(String, nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow, index=True)
    enabled_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    plugin = relationship("Plugin", back_populates="installations")


class PluginExecution(Base):
    """One general plugin invocation (not specifically a hook
    dispatch — see `PluginHookExecution` for those)."""
    __tablename__ = "plugin_executions"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    success = Column(Boolean, nullable=False, default=True)
    duration_ms = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow, index=True)
    metadata_ = Column("metadata", JSON, nullable=True)

    plugin = relationship("Plugin", back_populates="executions")


class PluginHookExecution(Base):
    """One hook dispatch to a plugin — its own table/index for
    high-volume hook-dispatch queries (see module docstring)."""
    __tablename__ = "plugin_hook_executions"

    id = Column(String, primary_key=True, default=_uuid)
    plugin_id = Column(String, ForeignKey("plugins.id"), nullable=False, index=True)
    hook = Column(String, nullable=False, index=True)
    success = Column(Boolean, nullable=False, default=True)
    duration_ms = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow, index=True)

    plugin = relationship("Plugin", back_populates="hook_executions")
