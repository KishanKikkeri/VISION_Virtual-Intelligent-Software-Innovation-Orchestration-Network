"""
services/repository/managers/audit_manager.py
================================================
Audit Manager.
Responsibilities: repository audit events, compliance logging.

Every state-changing operation in Repository Service must call
AuditManager.record_and_publish() exactly once — it writes the
append-only repository_events row AND fires the matching NATS event
in a single call so the two can never drift apart.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

from infrastructure.database.repositories import RepositoryEventRepository
from services.repository.managers import RepositoryDeps

log = structlog.get_logger(__name__)


class AuditManager:
    def __init__(self, deps: RepositoryDeps) -> None:
        self.deps = deps

    async def record_and_publish(
        self,
        event_type: str,
        repository_id: Optional[str] = None,
        project_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        actor: str = "VISION Bot",
    ) -> str:
        payload = payload or {}

        async with self.deps.db_factory() as db:
            event_id = await RepositoryEventRepository.record(
                db,
                event_type=event_type,
                repository_id=repository_id,
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                payload=payload,
            )

        if self.deps.nats is not None:
            try:
                await self.deps.nats.publish(event_type, {
                    "repository_id": repository_id,
                    "project_id":    project_id,
                    "entity_type":   entity_type,
                    "entity_id":     entity_id,
                    **payload,
                })
            except Exception as exc:
                log.warning("repository_event_publish_failed",
                            event_type=event_type, error=str(exc))
        else:
            log.debug("nats_unavailable_event_not_published", event_type=event_type)

        return event_id

    async def list_events(self, repository_id: str, limit: int = 100):
        async with self.deps.db_factory() as db:
            return await RepositoryEventRepository.list_for_repository(
                db, repository_id, limit=limit,
            )
