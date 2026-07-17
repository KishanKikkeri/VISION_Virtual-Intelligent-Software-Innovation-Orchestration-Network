"""
services/integration/replay/artifact_diff.py
=================================
M4.2 §4 Artifact Diff — compares two versions of the same
`(project_id, artifact_type)` artifact. The `artifacts` table
(infrastructure.database.models.Artifact) already carries a `version`
integer with a `UniqueConstraint("project_id", "artifact_type", "version")`
— i.e. every revision an artifact went through (each requirements
rewrite, each architecture-doc revision round) is already a distinct,
retained row, not overwritten in place. This module is the read-side
that makes that history useful: given two version numbers, it loads
both rows and reduces their `.content` JSONB to a `state_diff.StateDiff`,
plus a small amount of artifact-specific bookkeeping (status/approval
change, which `state_diff` alone wouldn't surface since it only looks
at `.content`).

Framework-free at its core (`diff_artifact_snapshots` takes plain dicts,
same as `state_diff.diff_states`); `diff_artifact_versions` is the
thin async wrapper that loads the two rows via
`ArtifactRepository.get_version`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel

from services.integration.replay.state_diff import StateDiff, diff_states


class ArtifactSnapshot(BaseModel):
    version: int
    status: str
    created_by: str
    created_at: Optional[str] = None
    approved_by: Optional[str] = None


class ArtifactDiff(BaseModel):
    project_id: str
    artifact_type: str
    from_version: int
    to_version: int
    from_snapshot: ArtifactSnapshot
    to_snapshot: ArtifactSnapshot
    status_changed: bool
    content_diff: StateDiff


def diff_artifact_snapshots(
    project_id: str, artifact_type: str,
    from_version: int, to_version: int,
    from_content: Dict[str, Any], to_content: Dict[str, Any],
    from_meta: ArtifactSnapshot, to_meta: ArtifactSnapshot,
) -> ArtifactDiff:
    content_diff = diff_states(from_content or {}, to_content or {})
    return ArtifactDiff(
        project_id=project_id, artifact_type=artifact_type,
        from_version=from_version, to_version=to_version,
        from_snapshot=from_meta, to_snapshot=to_meta,
        status_changed=from_meta.status != to_meta.status,
        content_diff=content_diff,
    )


def _to_snapshot(row: Any) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        version=row.version, status=row.status, created_by=row.created_by,
        created_at=row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        approved_by=row.approved_by,
    )


async def diff_artifact_versions(
    db: Any, project_id: str, artifact_type: str, from_version: int, to_version: int,
) -> Optional[ArtifactDiff]:
    """Returns None (rather than raising) if either version doesn't
    exist for this project/artifact_type — callers (the API route)
    turn that into a 404, same convention as
    version_registry.diff_versions returning None for an unknown version."""
    from infrastructure.database.repositories import ArtifactRepository  # local import: avoid import cycles

    from_row = await ArtifactRepository.get_version(db, project_id, artifact_type, from_version)
    to_row = await ArtifactRepository.get_version(db, project_id, artifact_type, to_version)
    if from_row is None or to_row is None:
        return None

    return diff_artifact_snapshots(
        project_id, artifact_type, from_version, to_version,
        from_row.content or {}, to_row.content or {},
        _to_snapshot(from_row), _to_snapshot(to_row),
    )
