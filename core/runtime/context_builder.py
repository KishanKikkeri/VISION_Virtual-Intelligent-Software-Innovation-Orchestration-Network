"""
core/runtime/context_builder.py
================================
AgentContextBuilder — assembles AgentContext before every agent run.
This is the component that gives all 53 agents shared company memory.

Without it: 53 isolated brains.
With it:    1 shared software company.

Fetches from:
  PostgreSQL  — project, workflow, artifacts, budget, dependency graph
  Qdrant      — relevant vector memories for the task type
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from core.runtime.context import AgentContext

log = structlog.get_logger(__name__)


class AgentContextBuilder:
    """
    Builds AgentContext objects.
    One instance per service, created at startup.
    Call build() before every agent task — result is immutable.
    """

    def __init__(self, db_factory, qdrant_client=None):
        self._db      = db_factory     # callable → async context manager → AsyncSession
        self._qdrant  = qdrant_client  # optional Qdrant client

    async def build(
        self,
        project_id:     str,
        task_type:      str,
        agent_id:       str,
        artifact_types: Optional[List[str]] = None,
        memory_limit:   int = 5,
    ) -> AgentContext:
        """
        Assembles the full AgentContext for a task.
        All infrastructure calls happen here — agents never touch DB directly.
        """
        artifact_types = artifact_types or []

        project, workflow_id, budget = await self._fetch_project_and_budget(project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")

        artifacts = await self._fetch_artifacts(project_id, artifact_types)
        memories  = await self._fetch_memories(project_id, task_type, memory_limit)
        dep_graph = await self._fetch_dependency_graph(project_id)

        ctx = AgentContext(
            project_id          =project_id,
            workflow_id         =workflow_id,
            current_phase       =project.get("current_phase", 1),
            project_name        =project.get("name", ""),
            project_description =project.get("description", ""),
            approved_artifacts  =artifacts,
            memory_snippets     =memories,
            tech_stack          =project.get("tech_stack", _DEFAULT_TECH_STACK),
            coding_standards    =project.get("coding_standards", _DEFAULT_STANDARDS),
            llm_provider        =project.get("llm_provider", "anthropic"),
            llm_model           =project.get("llm_model", "claude-sonnet-4-6"),
            budget_limit_usd    =budget.get("limit_usd"),
            total_spend_usd     =budget.get("total_spend_usd", 0.0),
            dependency_graph    =dep_graph,
        )

        log.debug("context_built",
                  project_id=project_id, task_type=task_type,
                  artifacts_loaded=len(artifacts), memories_loaded=len(memories))
        return ctx

    # ── Private fetch methods ─────────────────────────────────

    async def _fetch_project_and_budget(
        self, project_id: str
    ) -> tuple[Dict, str, Dict]:
        """Fetches project row + workflow ID + budget snapshot."""
        from sqlalchemy import func, select, text
        from infrastructure.database.models import (
            BudgetLimit, Project, TokenLedger, Workflow,
        )

        async with self._db() as db:
            # Project
            r = await db.execute(
                select(Project).where(Project.id == project_id)
            )
            p = r.scalar_one_or_none()
            if not p:
                return None, "", {}

            # Workflow ID
            w = await db.execute(
                select(Workflow.id).where(Workflow.project_id == project_id)
            )
            wf_id = w.scalar_one_or_none() or ""

            # Budget
            bl = await db.execute(
                select(BudgetLimit).where(BudgetLimit.project_id == project_id)
            )
            budget_row = bl.scalar_one_or_none()

            spend = await db.execute(
                select(func.coalesce(func.sum(TokenLedger.cost_usd), 0))
                .where(TokenLedger.project_id == project_id)
            )
            total_spend = float(spend.scalar_one())

            project_dict = {
                "id":            p.id,
                "name":          p.name,
                "description":   p.description,
                "current_phase": p.current_phase,
                "llm_provider":  p.llm_provider,
                "tech_stack":    _DEFAULT_TECH_STACK,
                "coding_standards": _DEFAULT_STANDARDS,
            }
            budget_dict = {
                "limit_usd":     float(budget_row.limit_usd) if budget_row and budget_row.limit_usd else None,
                "total_spend_usd": total_spend,
            }
            return project_dict, wf_id, budget_dict

    async def _fetch_artifacts(
        self, project_id: str, artifact_types: List[str]
    ) -> Dict[str, Any]:
        """
        Fetches latest approved version of each requested artifact type.
        Returns {artifact_type: parsed_content}.
        """
        if not artifact_types:
            return {}

        from sqlalchemy import select
        from infrastructure.database.models import Artifact
        from infrastructure.storage.base import get_storage

        storage = get_storage()
        result: Dict[str, Any] = {}

        async with self._db() as db:
            for atype in artifact_types:
                r = await db.execute(
                    select(Artifact)
                    .where(
                        Artifact.project_id    == project_id,
                        Artifact.artifact_type == atype,
                        Artifact.status        == "approved",
                    )
                    .order_by(Artifact.version.desc())
                    .limit(1)
                )
                artifact = r.scalar_one_or_none()
                if artifact:
                    if artifact.content:
                        result[atype] = artifact.content
                    elif artifact.storage_ref:
                        content = await storage.load(artifact.storage_ref)
                        if content:
                            result[atype] = content

        return result

    async def _fetch_memories(
        self, project_id: str, task_type: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Semantic search in Qdrant for task-relevant memories."""
        if not self._qdrant:
            return []
        try:
            query    = f"{task_type} {project_id}"
            results  = self._qdrant.search(
                collection_name=f"project_{project_id}",
                query_text=query,
                limit=limit,
                with_payload=True,
            )
            return [
                {
                    "content": r.payload.get("content", ""),
                    "score":   r.score,
                    "source":  r.payload.get("source", "unknown"),
                }
                for r in results
            ]
        except Exception:
            return []

    async def _fetch_dependency_graph(
        self, project_id: str
    ) -> List[Dict[str, Any]]:
        """Fetches entity relationships from requirement_dependencies."""
        try:
            from sqlalchemy import text
            async with self._db() as db:
                r = await db.execute(
                    text("""
                        SELECT source_entity_type, source_entity_id,
                               relationship_type, target_entity_type, target_entity_id
                        FROM requirement_dependencies
                        WHERE project_id = :pid
                        LIMIT 100
                    """),
                    {"pid": project_id},
                )
                return [
                    {
                        "source_type": row[0], "source_id": str(row[1]),
                        "relationship": row[2],
                        "target_type": row[3], "target_id": str(row[4]),
                    }
                    for row in r.fetchall()
                ]
        except Exception:
            return []


# ── Defaults ──────────────────────────────────────────────────

_DEFAULT_TECH_STACK: Dict[str, str] = {
    "backend":      "Python + FastAPI",
    "agent_runtime":"LangGraph + custom orchestration",
    "database":     "PostgreSQL",
    "vector_memory":"Qdrant",
    "messaging":    "NATS JetStream",
    "frontend":     "Next.js + TypeScript + Tailwind",
    "llm_layer":    "BaseLLMProvider abstraction",
    "containerization": "Docker + Docker Compose",
}

_DEFAULT_STANDARDS: List[str] = [
    "Type hints on all function signatures",
    "Async/await throughout — no blocking calls",
    "Pydantic v2 for all data models",
    "Structured logging via structlog",
    "No hardcoded secrets or credentials",
    "All DB access via repository pattern",
    "Single responsibility per agent",
]
