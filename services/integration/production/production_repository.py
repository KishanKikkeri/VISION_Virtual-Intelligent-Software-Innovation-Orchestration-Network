"""
services/integration/production/production_repository.py
=================================
M4.9 §9 Repository — every other module in this package only ever
imports `ProductionRepository` (never
`infrastructure.database.production_models` directly), same convention
`designer_repository.py`/`plugin_repository.py` set for their own
callers.
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import select

from services.integration.production.release_models import (
    BackupRecord, BackupScope, BackupType, DeploymentProfile, Environment, EnvironmentReport, Release, RestoreMode,
    RestoreRecord,
)


class ProductionRepository:

    # ── Releases ─────────────────────────────────────────────────────

    @staticmethod
    async def save_release(db: Any, release: Release) -> str:
        from infrastructure.database.production_models import ReleaseRow

        result = await db.execute(select(ReleaseRow).where(ReleaseRow.version == release.version))
        existing = result.scalar_one_or_none()
        payload = release.model_dump(mode="json")
        if existing is None:
            db.add(ReleaseRow(version=release.version, previous_version=release.previous_version,
                               channel=release.channel, release_json=payload))
        else:
            existing.release_json = payload
            existing.channel = release.channel
        await db.flush()
        return release.version

    @staticmethod
    async def get_release(db: Any, version: str) -> Optional[Release]:
        from infrastructure.database.production_models import ReleaseRow

        result = await db.execute(select(ReleaseRow).where(ReleaseRow.version == version))
        row = result.scalar_one_or_none()
        return Release.model_validate(row.release_json) if row is not None else None

    @staticmethod
    async def list_releases(db: Any) -> List[Release]:
        from infrastructure.database.production_models import ReleaseRow

        result = await db.execute(select(ReleaseRow).order_by(ReleaseRow.created_at.desc()))
        return [Release.model_validate(r.release_json) for r in result.scalars().all()]

    @staticmethod
    async def latest_release(db: Any) -> Optional[Release]:
        releases = await ProductionRepository.list_releases(db)
        return releases[0] if releases else None

    # ── Deployment profile snapshots ──────────────────────────────────

    @staticmethod
    async def save_profile_snapshot(db: Any, profile: DeploymentProfile) -> str:
        from infrastructure.database.production_models import DeploymentProfileRow

        row = DeploymentProfileRow(environment=profile.environment.value if isinstance(profile.environment, Environment)
                                    else profile.environment, profile_json=profile.model_dump(mode="json"))
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_profile_snapshots(db: Any, environment: str, limit: int = 20) -> List[DeploymentProfile]:
        from infrastructure.database.production_models import DeploymentProfileRow

        result = await db.execute(
            select(DeploymentProfileRow).where(DeploymentProfileRow.environment == environment)
            .order_by(DeploymentProfileRow.created_at.desc()).limit(limit)
        )
        return [DeploymentProfile.model_validate(r.profile_json) for r in result.scalars().all()]

    # ── Backups ────────────────────────────────────────────────────────

    @staticmethod
    async def save_backup_record(db: Any, record: BackupRecord) -> str:
        from infrastructure.database.production_models import BackupRecordRow

        row = BackupRecordRow(id=record.id, backup_type=record.backup_type.value, scopes=[s.value for s in record.scopes],
                               location=record.location, checksum=record.checksum, size_bytes=record.size_bytes,
                               status=record.status, baseline_backup_id=record.baseline_backup_id, notes=record.notes)
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def get_backup_record(db: Any, backup_id: str) -> Optional[BackupRecord]:
        from infrastructure.database.production_models import BackupRecordRow

        result = await db.execute(select(BackupRecordRow).where(BackupRecordRow.id == backup_id))
        r = result.scalar_one_or_none()
        if r is None:
            return None
        return BackupRecord(id=r.id, backup_type=BackupType(r.backup_type), scopes=[BackupScope(s) for s in r.scopes],
                             location=r.location, checksum=r.checksum, size_bytes=r.size_bytes, status=r.status,
                             baseline_backup_id=r.baseline_backup_id, notes=r.notes or [],
                             created_at=r.created_at.isoformat() if r.created_at else "")

    @staticmethod
    async def list_backup_records(db: Any, limit: int = 50) -> List[BackupRecord]:
        from infrastructure.database.production_models import BackupRecordRow

        result = await db.execute(select(BackupRecordRow).order_by(BackupRecordRow.created_at.desc()).limit(limit))
        return [
            BackupRecord(id=r.id, backup_type=BackupType(r.backup_type), scopes=[BackupScope(s) for s in r.scopes],
                          location=r.location, checksum=r.checksum, size_bytes=r.size_bytes, status=r.status,
                          baseline_backup_id=r.baseline_backup_id, notes=r.notes or [],
                          created_at=r.created_at.isoformat() if r.created_at else "")
            for r in result.scalars().all()
        ]

    # ── Restores ───────────────────────────────────────────────────────

    @staticmethod
    async def save_restore_record(db: Any, record: RestoreRecord) -> str:
        from infrastructure.database.production_models import RestoreRecordRow

        row = RestoreRecordRow(id=record.id, backup_id=record.backup_id, mode=record.mode.value,
                                scopes=[s.value for s in record.scopes], status=record.status,
                                confirmed=record.confirmed, validation_issues=record.validation_issues)
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_restore_records(db: Any, limit: int = 50) -> List[RestoreRecord]:
        from infrastructure.database.production_models import RestoreRecordRow

        result = await db.execute(select(RestoreRecordRow).order_by(RestoreRecordRow.created_at.desc()).limit(limit))
        return [
            RestoreRecord(id=r.id, backup_id=r.backup_id, mode=RestoreMode(r.mode),
                           scopes=[BackupScope(s) for s in r.scopes], status=r.status, confirmed=r.confirmed,
                           validation_issues=r.validation_issues or [],
                           created_at=r.created_at.isoformat() if r.created_at else "")
            for r in result.scalars().all()
        ]

    # ── Environment checks ──────────────────────────────────────────────

    @staticmethod
    async def save_environment_check(db: Any, report: EnvironmentReport) -> str:
        from infrastructure.database.production_models import EnvironmentCheckRow

        row = EnvironmentCheckRow(environment=report.environment, overall_status=report.overall_status.value,
                                   report_json=report.model_dump(mode="json"))
        db.add(row)
        await db.flush()
        return row.id

    @staticmethod
    async def list_environment_checks(db: Any, environment: str, limit: int = 20) -> List[EnvironmentReport]:
        from infrastructure.database.production_models import EnvironmentCheckRow

        result = await db.execute(
            select(EnvironmentCheckRow).where(EnvironmentCheckRow.environment == environment)
            .order_by(EnvironmentCheckRow.created_at.desc()).limit(limit)
        )
        return [EnvironmentReport.model_validate(r.report_json) for r in result.scalars().all()]

    @staticmethod
    async def latest_environment_check(db: Any, environment: str) -> Optional[EnvironmentReport]:
        reports = await ProductionRepository.list_environment_checks(db, environment, limit=1)
        return reports[0] if reports else None


async def fetch_production_dashboard_section(db: Any = None, environment: str = "production") -> Optional[dict]:
    """M4.9's Dashboard Integration analog to M4.7/M4.8's own
    `fetch_plugin_dashboard_section`/`fetch_designer_dashboard_section`
    — the one function `dashboard_builder.build_production_summary`
    consumes."""
    if db is None:
        return {"latest_release_version": None, "latest_backup_at": None, "latest_environment_status": None,
                "backup_count": 0}

    import structlog
    log = structlog.get_logger(__name__)
    try:
        latest_release = await ProductionRepository.latest_release(db)
        backups = await ProductionRepository.list_backup_records(db, limit=100)
        latest_check = await ProductionRepository.latest_environment_check(db, environment)
    except Exception as e:  # noqa: BLE001 — degrades this card, not the whole dashboard
        log.info("production_dashboard_history_unavailable", error=str(e))
        return None

    return {
        "latest_release_version": latest_release.version if latest_release else None,
        "latest_backup_at": backups[0].created_at if backups else None,
        "backup_count": len(backups),
        "latest_environment_status": latest_check.overall_status.value if latest_check else None,
    }
