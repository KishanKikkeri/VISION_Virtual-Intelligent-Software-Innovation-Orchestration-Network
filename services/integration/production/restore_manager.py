"""
services/integration/production/restore_manager.py
=================================
M4.9 §7 Restore Manager — `dry_run`/`full`/`selective` restore,
validation, and "never overwrite data without confirmation."

**The confirmation gate is structural, not a caller convention**:
`apply_restore` raises `RestoreNotConfirmedError` for any mode other
than `dry_run` unless `confirm=True` is passed explicitly by the
caller — there is no way to reach the "write scopes back" branch
without that flag, so a CLI/API caller has to deliberately opt in
(same spirit as `production_cli.py`'s own `--yes` flag requirement for
its `restore` command).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.integration.production.backup_manager import load_backup_payload, verify_backup
from services.integration.production.release_models import BackupRecord, BackupScope, RestoreMode, RestoreRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RestoreNotConfirmedError(Exception):
    pass


def _validate(record: BackupRecord, scopes: List[BackupScope]) -> List[str]:
    issues: List[str] = []
    if not verify_backup(record):
        issues.append(f"backup {record.id!r} failed checksum verification; it may be corrupted")
    missing = [s.value for s in scopes if s not in record.scopes]
    if missing:
        issues.append(f"requested scope(s) not present in this backup: {missing}")
    if record.status == "partial":
        issues.append(f"backup {record.id!r} was recorded as 'partial'; some scopes may be incomplete")
    return issues


def plan_restore(backup: BackupRecord, mode: RestoreMode = RestoreMode.DRY_RUN,
                  scopes: Optional[List[BackupScope]] = None) -> RestoreRecord:
    """Builds a `RestoreRecord` and runs validation, but never writes
    anything — safe to call for any mode, including a real `full`/
    `selective` restore that will later be handed to `apply_restore`."""
    target_scopes = scopes if scopes is not None else list(backup.scopes)
    issues = _validate(backup, target_scopes)
    status = "validated" if not issues else "rejected"
    return RestoreRecord(
        id=str(uuid.uuid4()),
        backup_id=backup.id,
        mode=mode,
        scopes=target_scopes,
        status="planned" if mode == RestoreMode.DRY_RUN else status,
        confirmed=False,
        validation_issues=issues,
        created_at=_now_iso(),
    )


def apply_restore(
    plan: RestoreRecord,
    backup: BackupRecord,
    writers: Optional[Dict[BackupScope, Callable[[Any], None]]] = None,
    confirm: bool = False,
) -> RestoreRecord:
    """Applies a previously-planned `full`/`selective` restore.
    Raises `RestoreNotConfirmedError` unless `confirm=True`; raises
    `ValueError` for a `dry_run` plan (dry runs are never applied —
    call `plan_restore` again with a real mode instead)."""
    if plan.mode == RestoreMode.DRY_RUN:
        raise ValueError("a dry_run restore plan cannot be applied; build a full/selective plan instead")
    if not confirm:
        raise RestoreNotConfirmedError("apply_restore requires confirm=True; restores never overwrite data silently")
    if plan.validation_issues:
        return plan.model_copy(update={"status": "failed", "confirmed": True})

    writers = writers or {}
    payload = load_backup_payload(backup)
    applied: List[str] = []
    for scope in plan.scopes:
        writer = writers.get(scope)
        if writer is None:
            continue
        writer(payload.get(scope.value))
        applied.append(scope.value)

    return plan.model_copy(update={"status": "applied", "confirmed": True})


def dry_run_restore(backup: BackupRecord, scopes: Optional[List[BackupScope]] = None) -> RestoreRecord:
    """Convenience wrapper — §7's explicit "dry run" mode."""
    return plan_restore(backup, mode=RestoreMode.DRY_RUN, scopes=scopes)
