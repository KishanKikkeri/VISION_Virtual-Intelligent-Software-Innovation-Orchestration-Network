"""
infrastructure/database/production_models.py
=================================
M4.9 §9 Repository — five new, additive-only tables, named exactly per
the brief:

    releases              — one row per cut `Release` (brief's `Release`).
    deployment_profiles    — one row per named+overrides `DeploymentProfile`
                           snapshot taken (brief's `DeploymentProfile`);
                           the *live* profile is always computed on the
                           fly by `deployment_profiles.get_profile` — a
                           row here is a recorded/audited snapshot, same
                           "computed value vs. recorded snapshot" split
                           `canvas_snapshots` (M4.8) established for
                           `WorkflowLayoutRow`.
    backup_records         — one row per `BackupRecord` (brief's
                           `BackupRecord`); the actual backup payload
                           lives on disk (see `backup_manager.py`), this
                           row is the durable index/metadata over it.
    restore_records         — one row per `RestoreRecord` (brief's
                           `RestoreRecord`).
    environment_checks      — one row per `run_environment_checks()` call
                           (brief's `EnvironmentCheck`), storing the full
                           `EnvironmentReport` as JSON for history/trend.

**Scope note — identical situation to every prior M4.x DB models file in
this slice:** written against a sandbox slice that does not include the
platform's real `infrastructure/database/models.py`. Tries the real
`Base` first, falling back to a local one only for this milestone's own
tests. Before this ships: merge these five class bodies into the real
`models.py` (or confirm the import resolves) — see
`docs/M4.9_Production_Readiness_Handover.md` §3.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import relationship

try:
    from infrastructure.database.models import Base  # the real declarative base, once available
except Exception:  # noqa: BLE001 — see module docstring
    from sqlalchemy.orm import declarative_base
    Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


class ReleaseRow(Base):
    __tablename__ = "releases"

    version = Column(String, primary_key=True)
    previous_version = Column(String, nullable=True)
    channel = Column(String, nullable=False, default="stable")
    release_json = Column(JSON, nullable=False)  # full Release.model_dump(mode="json")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class DeploymentProfileRow(Base):
    __tablename__ = "deployment_profiles"

    id = Column(String, primary_key=True, default=_uuid)
    environment = Column(String, nullable=False, index=True)
    profile_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class BackupRecordRow(Base):
    __tablename__ = "backup_records"

    id = Column(String, primary_key=True, default=_uuid)
    backup_type = Column(String, nullable=False, default="full")
    scopes = Column(JSON, nullable=False)
    location = Column(String, nullable=False)
    checksum = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="completed")
    baseline_backup_id = Column(String, nullable=True)
    notes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class RestoreRecordRow(Base):
    __tablename__ = "restore_records"

    id = Column(String, primary_key=True, default=_uuid)
    backup_id = Column(String, nullable=False, index=True)
    mode = Column(String, nullable=False, default="dry_run")
    scopes = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="planned")
    confirmed = Column(Boolean, nullable=False, default=False)
    validation_issues = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class EnvironmentCheckRow(Base):
    __tablename__ = "environment_checks"

    id = Column(String, primary_key=True, default=_uuid)
    environment = Column(String, nullable=False, index=True)
    overall_status = Column(String, nullable=False, default="skipped")
    report_json = Column(JSON, nullable=False)  # full EnvironmentReport.model_dump(mode="json")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
