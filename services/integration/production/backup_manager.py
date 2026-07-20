"""
services/integration/production/backup_manager.py
=================================
M4.9 §6 Backup Manager — backs up `database` / `artifacts` /
`workflow_layouts` / `plugin_registry` / `configuration` / `audit_logs`
(the six `BackupScope` values), `full` or `incremental`.

**No storage backend is assumed.** `create_backup` takes a
`fetchers: Dict[BackupScope, Callable[[], Any]]` map — each scope's
own function for producing that scope's payload (e.g.
`DesignerRepository.list_layouts` for `WORKFLOW_LAYOUTS`,
`PluginRepository.list_plugins` for `PLUGIN_REGISTRY`). A scope with no
fetcher supplied is skipped (recorded in `BackupRecord.notes`, not
silently dropped) — same graceful-degradation convention every other
M4.9 module uses. The resulting payload is written to `destination_dir`
as one JSON file per backup; that's real disk I/O the tests exercise
against a tmp_path, not a stub.

**Incremental backups** only re-fetch scopes not already covered by
`baseline_backup_id`'s own scope list (§6 "Support... incremental" —
the practical reading of "incremental" for a set of independent,
already-append-only scopes: skip re-capturing what the baseline already
holds, rather than a byte-level diff, which the `configuration`/`
audit_logs`/`workflow_layouts` scopes have no meaningful byte-diff
representation for in the first place).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.integration.production.release_models import BackupRecord, BackupScope, BackupType


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_backup(
    scopes: List[BackupScope],
    destination_dir: str,
    fetchers: Optional[Dict[BackupScope, Callable[[], Any]]] = None,
    backup_type: BackupType = BackupType.FULL,
    baseline: Optional[BackupRecord] = None,
) -> BackupRecord:
    """Writes `{destination_dir}/{backup_id}.json` containing each
    requested scope's fetched payload (or `null` + a note when no
    fetcher was supplied / a fetcher raised)."""
    fetchers = fetchers or {}
    os.makedirs(destination_dir, exist_ok=True)

    scopes_to_capture = list(scopes)
    notes: List[str] = []
    if backup_type == BackupType.INCREMENTAL:
        if baseline is None:
            notes.append("no baseline supplied; incremental backup captured all requested scopes")
        else:
            already_covered = set(baseline.scopes)
            scopes_to_capture = [s for s in scopes if s not in already_covered]
            skipped = [s.value for s in scopes if s in already_covered]
            if skipped:
                notes.append(f"skipped scopes already covered by baseline {baseline.id!r}: {skipped}")

    payload: Dict[str, Any] = {}
    captured_scopes: List[BackupScope] = []
    for scope in scopes_to_capture:
        fetcher = fetchers.get(scope)
        if fetcher is None:
            payload[scope.value] = None
            notes.append(f"no fetcher supplied for scope {scope.value!r}; recorded as empty")
            continue
        try:
            payload[scope.value] = fetcher()
            captured_scopes.append(scope)
        except Exception as e:  # noqa: BLE001
            payload[scope.value] = None
            notes.append(f"fetching scope {scope.value!r} failed: {e}")

    backup_id = str(uuid.uuid4())
    checksum = _checksum(payload)
    location = os.path.join(destination_dir, f"{backup_id}.json")
    with open(location, "w", encoding="utf-8") as f:
        json.dump({"backup_id": backup_id, "checksum": checksum, "scopes": [s.value for s in scopes_to_capture],
                   "payload": payload}, f, indent=2, default=str)

    status = "completed" if captured_scopes or not scopes_to_capture else "partial"
    if scopes_to_capture and not captured_scopes:
        status = "partial"

    return BackupRecord(
        id=backup_id,
        backup_type=backup_type,
        scopes=captured_scopes if backup_type == BackupType.INCREMENTAL else scopes_to_capture,
        location=location,
        checksum=checksum,
        size_bytes=os.path.getsize(location),
        status=status,
        baseline_backup_id=baseline.id if baseline else None,
        notes=notes,
        created_at=_now_iso(),
    )


def load_backup_payload(record: BackupRecord) -> Dict[str, Any]:
    """Reads a backup's payload back off disk (used by
    `restore_manager.py`)."""
    if not os.path.isfile(record.location):
        raise FileNotFoundError(f"backup file not found: {record.location}")
    with open(record.location, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("payload", {})


def verify_backup(record: BackupRecord) -> bool:
    """Recomputes the checksum from the file on disk and compares
    against the recorded one — cheap corruption/tamper detection."""
    try:
        payload = load_backup_payload(record)
    except Exception:  # noqa: BLE001
        return False
    return _checksum(payload) == record.checksum
