"""
services/integration/api/production.py
=================================
M4.9 §10 API section:

    GET  /production/status
    GET  /production/checks
    POST /production/validate
    GET  /production/releases
    POST /production/releases
    POST /production/backup
    POST /production/restore
    GET  /production/backups
    GET  /production/environment
    GET  /production/config
    POST /production/config/validate
    GET  /production/checklist

Own `APIRouter`, same "new endpoints, no rewrites" convention every
prior M4.x API module in this slice established. Persistence follows
the DB-preferred-with-in-memory-fallback pattern `api/designer.py`/
`api/plugins.py` use: when `request.app.state.db_factory` isn't
configured, releases/backups/restores/environment-checks are kept in
process-local lists on `request.app.state` instead of failing the
request.

**`POST /production/restore` never actually applies a restore.** It
only ever calls `restore_manager.plan_restore` (i.e. always dry-run or
validate-only) — applying a real restore is deliberately not exposed
over HTTP in this milestone (see `restore_manager.py`'s confirmation
gate); that stays a CLI-only, explicitly-confirmed operation per
§Critical Constraints' "never overwrite data without confirmation."
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Query, Request

from services.integration.production import (
    backup_manager, configuration_manager, deployment_profiles, deployment_validator, environment_validator,
    release_manager,
)
from services.integration.production.release_models import (
    BackupScope, BackupType, ProductionChecklist, ProductionChecklistItem,
    ProductionStatus, RestoreMode,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/production", tags=["Production Readiness"])


def _db_factory(request: Request) -> Optional[Any]:
    return getattr(request.app.state, "db_factory", None)


def _memory(request: Request, attr: str) -> List[Any]:
    if not hasattr(request.app.state, attr):
        setattr(request.app.state, attr, [])
    return getattr(request.app.state, attr)


@router.get("/environment")
async def get_environment(request: Request, environment: str = Query("production")) -> Dict[str, Any]:
    """§3 — a live sweep using whatever connectors are wired on app
    state (`request.app.state.db_ping`, etc.); absent connectors simply
    yield `SKIPPED` checks rather than an error."""
    report = environment_validator.run_environment_checks(
        environment,
        db_ping=getattr(request.app.state, "db_ping", None),
        cache_ping=getattr(request.app.state, "cache_ping", None),
        messaging_ping=getattr(request.app.state, "messaging_ping", None),
    )
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                await ProductionRepository.save_environment_check(db, report)
        except Exception as e:  # noqa: BLE001
            log.warning("environment_check_persist_failed", error=str(e))
    else:
        _memory(request, "production_environment_checks").append(report)
    return report.model_dump(mode="json")


@router.get("/checks")
async def get_checks(request: Request, environment: str = Query("production")) -> Dict[str, Any]:
    """History of past environment checks (§3), same "absence is not
    an error" convention every optional-integration route in this
    slice uses."""
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                reports = await ProductionRepository.list_environment_checks(db, environment)
            return {"environment": environment, "checks": [r.model_dump(mode="json") for r in reports]}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read environment check history: {e}")
    reports = [r for r in _memory(request, "production_environment_checks") if r.environment == environment]
    return {"environment": environment, "checks": [r.model_dump(mode="json") for r in reports]}


@router.post("/validate")
async def validate_deployment_route(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """§4 — `body` is `{"assets": {name: text, ...}, "environment": "...", "environment_config": {...}}`."""
    assets = body.get("assets", {})
    environment = body.get("environment", "production")
    env_config = body.get("environment_config")
    try:
        result = deployment_validator.validate_deployment(assets, environment, env_config)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"could not validate deployment assets: {e}")
    return result.model_dump(mode="json")


@router.get("/releases")
async def list_releases(request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                releases = await ProductionRepository.list_releases(db)
            return {"count": len(releases), "releases": [r.model_dump(mode="json") for r in releases]}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read releases: {e}")
    releases = _memory(request, "production_releases")
    return {"count": len(releases), "releases": [r.model_dump(mode="json") for r in releases]}


@router.post("/releases")
async def create_release(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    version = body.get("version")
    if not version:
        raise HTTPException(status_code=422, detail="body must include 'version'")
    try:
        release = release_manager.build_release(
            version=version, previous_version=body.get("previous_version"),
            release_notes=body.get("release_notes"), channel=body.get("channel", "stable"),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                await ProductionRepository.save_release(db, release)
        except Exception as e:  # noqa: BLE001
            log.warning("release_persist_failed", version=version, error=str(e))
    else:
        _memory(request, "production_releases").append(release)
    return release.model_dump(mode="json")


@router.post("/backup")
async def create_backup_route(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    scopes_in = body.get("scopes") or [s.value for s in BackupScope]
    try:
        scopes = [BackupScope(s) for s in scopes_in]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    destination = body.get("destination_dir", "/tmp/aasc_backups")
    backup_type = BackupType(body.get("backup_type", "full"))
    record = backup_manager.create_backup(scopes, destination, backup_type=backup_type)

    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                await ProductionRepository.save_backup_record(db, record)
        except Exception as e:  # noqa: BLE001
            log.warning("backup_record_persist_failed", error=str(e))
    else:
        _memory(request, "production_backups").append(record)
    return record.model_dump(mode="json")


@router.get("/backups")
async def list_backups(request: Request) -> Dict[str, Any]:
    db_factory = _db_factory(request)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                records = await ProductionRepository.list_backup_records(db)
            return {"count": len(records), "backups": [r.model_dump(mode="json") for r in records]}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read backups: {e}")
    records = _memory(request, "production_backups")
    return {"count": len(records), "backups": [r.model_dump(mode="json") for r in records]}


@router.post("/restore")
async def plan_restore_route(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """§7 — plan-only (see module docstring): builds and returns a
    `RestoreRecord` (dry-run by default) without ever applying it."""
    from services.integration.production import restore_manager

    backup_id = body.get("backup_id")
    if not backup_id:
        raise HTTPException(status_code=422, detail="body must include 'backup_id'")
    mode = RestoreMode(body.get("mode", "dry_run"))
    scopes = [BackupScope(s) for s in body["scopes"]] if body.get("scopes") else None

    db_factory = _db_factory(request)
    backup = None
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                backup = await ProductionRepository.get_backup_record(db, backup_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read backup record: {e}")
    else:
        backup = next((b for b in _memory(request, "production_backups") if b.id == backup_id), None)

    if backup is None:
        raise HTTPException(status_code=404, detail=f"backup {backup_id!r} not found")

    plan = restore_manager.plan_restore(backup, mode=mode, scopes=scopes)
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                await ProductionRepository.save_restore_record(db, plan)
        except Exception as e:  # noqa: BLE001
            log.warning("restore_record_persist_failed", error=str(e))
    else:
        _memory(request, "production_restores").append(plan)
    return plan.model_dump(mode="json")


@router.get("/config")
async def get_config(environment: str = Query("production")) -> Dict[str, Any]:
    """§1/§2 — the fully-merged `DeploymentProfile` for `environment`."""
    try:
        profile = deployment_profiles.get_profile(environment)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return profile.model_dump(mode="json")


@router.post("/config/validate")
async def validate_config_route(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    config = body.get("config", {})
    environment = body.get("environment", "production")
    try:
        result = configuration_manager.validate(config, environment)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result.model_dump(mode="json")


@router.get("/checklist")
async def get_checklist(request: Request, environment: str = Query("production")) -> Dict[str, Any]:
    """§9 Production Checklist — a synthesized readiness view over the
    latest environment check / release / backup already recorded (§API
    `GET /production/checklist`); absence of any of those degrades that
    one item to `satisfied=False`, not an error."""
    db_factory = _db_factory(request)
    latest_check = latest_backup = None
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import ProductionRepository
            async with db_factory() as db:
                latest_check = await ProductionRepository.latest_environment_check(db, environment)
                backups = await ProductionRepository.list_backup_records(db, limit=1)
                latest_backup = backups[0] if backups else None
        except Exception as e:  # noqa: BLE001
            log.warning("checklist_history_unavailable", error=str(e))
    else:
        checks = [c for c in _memory(request, "production_environment_checks") if c.environment == environment]
        latest_check = checks[-1] if checks else None
        backups = _memory(request, "production_backups")
        latest_backup = backups[-1] if backups else None

    items = [
        ProductionChecklistItem(key="environment_ready", label="Environment checks pass",
                                 satisfied=bool(latest_check and latest_check.ready),
                                 detail=latest_check.overall_status.value if latest_check else "no check run yet"),
        ProductionChecklistItem(key="recent_backup", label="A backup has been taken",
                                 satisfied=bool(latest_backup),
                                 detail=latest_backup.created_at if latest_backup else "no backup recorded yet"),
    ]
    checklist = ProductionChecklist(environment=environment, items=items)
    return checklist.model_dump(mode="json")


@router.get("/status")
async def get_status(request: Request, environment: str = Query("production")) -> Dict[str, Any]:
    """§API `GET /production/status` — the single-glance
    `ProductionStatus` payload. Falls back to the same process-local
    in-memory stores every other route in this router uses when no
    `db_factory` is configured, so this endpoint reflects releases/
    backups created earlier in the same process even without a
    database wired."""
    from datetime import datetime, timezone

    db_factory = _db_factory(request)
    section: Optional[Dict[str, Any]] = None
    latest_backup_id: Optional[str] = None
    if db_factory is not None:
        try:
            from services.integration.production.production_repository import fetch_production_dashboard_section
            async with db_factory() as db:
                section = await fetch_production_dashboard_section(db, environment)
        except Exception as e:  # noqa: BLE001
            log.warning("production_status_history_unavailable", error=str(e))
    else:
        releases = _memory(request, "production_releases")
        backups = _memory(request, "production_backups")
        section = {
            "latest_release_version": releases[-1].version if releases else None,
            "latest_backup_at": backups[-1].created_at if backups else None,
        }
        latest_backup_id = backups[-1].id if backups else None

    status = ProductionStatus(
        environment=environment,
        generated_at=datetime.now(timezone.utc).isoformat(),
        latest_release_version=(section or {}).get("latest_release_version"),
        latest_backup_id=latest_backup_id,
        latest_backup_at=(section or {}).get("latest_backup_at"),
    )
    return status.model_dump(mode="json")
