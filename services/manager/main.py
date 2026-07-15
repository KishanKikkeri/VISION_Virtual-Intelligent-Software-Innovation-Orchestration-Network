"""
services/manager/main.py
=========================
Manager Service — the CEO service.
Runs W01 (Lifecycle Graph) and W12 (Delegation Graph).
Owns approval gates, budget enforcement, and escalations.

Key endpoints:
  POST /projects/start          — starts the full lifecycle graph
  POST /projects/{id}/approve   — injects approval decision and resumes graph
  POST /projects/{id}/reject    — injects rejection + feedback, resumes graph
  GET  /projects/{id}/state     — current graph state snapshot
  POST /delegate                — dispatches a single task via W12
  WS   /ws/{project_id}         — real-time event stream
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.config.settings import get_settings
from core.runtime.context import AgentContext, TaskInput
from core.runtime.context_builder import AgentContextBuilder
from core.runtime.factory import AgentFactory
from infrastructure.auth.jwt_auth import get_current_user_id
from infrastructure.database.connection import (
    close_db, get_db, get_db_context, init_db,
)
from infrastructure.database.repositories import (
    ArtifactRepository, AuditRepository, ProjectRepository,
    TokenLedgerRepository, UserRepository,
)
from infrastructure.messaging.nats_client import get_nats, init_nats
from infrastructure.monitoring.telemetry import configure_telemetry, ws_connections
from infrastructure.secrets.validator import validate_secrets
from infrastructure.storage.base import get_storage, init_storage
from infrastructure.websocket.manager import ws_manager
from services.manager.graphs.delegation import DelegationState, build_delegation_graph
from services.manager.graphs.lifecycle import LifecycleState, build_lifecycle_graph

log = structlog.get_logger(__name__)
settings = get_settings()

# ── Graph instances (compiled once at startup) ────────────────
_lifecycle_graph  = None
_delegation_graph = None
_agent_factory    = None
_context_builder  = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _lifecycle_graph, _delegation_graph, _agent_factory, _context_builder

    validate_secrets(exit_on_failure=True)
    configure_telemetry(metrics_port=9101)
    await init_db()

    try:
        await init_nats(settings.nats_url)
        # Subscribe to department completion events
        await _setup_nats_subscriptions()
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    init_storage(backend=settings.storage_backend,
                 base_path=settings.storage_local_path)

    from core.llm.registry import LLMProviderRegistry
    LLMProviderRegistry.initialise()

    # Build graphs
    _lifecycle_graph  = build_lifecycle_graph()
    _delegation_graph = build_delegation_graph()

    # Build agent factory
    _agent_factory = AgentFactory(
        db_factory=get_db_context,
        nats=_get_nats_safe(),
        storage=get_storage(),
        audit_repo=AuditRepository,
        artifact_repo=ArtifactRepository,
        token_repo=TokenLedgerRepository,
    )

    _context_builder = AgentContextBuilder(db_factory=get_db_context)

    log.info("manager_service_ready")
    yield

    try:
        await get_nats().drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC Manager Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _get_nats_safe():
    try:
        return get_nats()
    except RuntimeError:
        return None


async def _setup_nats_subscriptions():
    """Subscribe to department completion events to advance the lifecycle graph."""
    nats = get_nats()

    async def on_requirements_completed(payload: Dict[str, Any]):
        project_id = payload.get("project_id")
        if project_id and _lifecycle_graph:
            config = {"configurable": {"thread_id": project_id}}
            _lifecycle_graph.update_state(config, {
                "phase_status":    "completed",
                "approval_status": "pending",
            })
            log.info("requirements_completed_received", project_id=project_id)

    await nats.subscribe("product.requirements.completed",
                         on_requirements_completed,
                         durable="manager-requirements-completed")


# ═══════════════════════════════════════════════════════════════
# REQUEST/RESPONSE SCHEMAS
# ═══════════════════════════════════════════════════════════════

class StartProjectRequest(BaseModel):
    project_id:   str
    name:         str
    description:  str
    budget_usd:   Optional[float] = None
    llm_provider: str = "anthropic"


class ApprovalRequest(BaseModel):
    feedback:     Optional[str] = None


class RejectionRequest(BaseModel):
    feedback:     str    # required — must explain what to fix


class DelegateTaskRequest(BaseModel):
    project_id:   str
    task_type:    str
    description:  str
    priority:     int = 5
    max_retries:  int = 3


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health", tags=["System"])
async def health():
    from infrastructure.database.connection import check_db_health
    return {
        "status":  "ok",
        "service": "manager",
        "graphs":  {
            "lifecycle":  _lifecycle_graph is not None,
            "delegation": _delegation_graph is not None,
        },
        "db":     await check_db_health(),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
# LIFECYCLE — W01
# ═══════════════════════════════════════════════════════════════

@app.post("/projects/start", tags=["Lifecycle"])
async def start_project(
    req:     StartProjectRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Starts W01 lifecycle graph for a project.
    The graph runs until it hits the requirements approval gate.
    """
    if not _lifecycle_graph:
        raise HTTPException(503, "Lifecycle graph not initialised")

    initial_state: LifecycleState = {
        "project_id":            req.project_id,
        "workflow_id":           str(uuid.uuid4()),
        "owner_id":              user_id,
        "current_phase":         0,
        "phase_status":          "pending",
        "active_tasks":          [],
        "completed_tasks":       [],
        "failed_tasks":          [],
        "artifacts":             {},
        "awaiting_approval":     False,
        "approval_artifact_type":None,
        "approval_status":       None,
        "approval_feedback":     None,
        "revision_round":        0,
        "budget_limit_usd":      req.budget_usd,
        "total_spend_usd":       0.0,
        "budget_status":         "active",
        "retry_count":           0,
        "failure_reason":        None,
        "escalation_required":   False,
        "nats_events_queue":     [],
        "websocket_events_queue":[],
    }

    config = {"configurable": {"thread_id": req.project_id}}

    # Run graph — will pause at requirements_approval_gate
    result = _lifecycle_graph.invoke(initial_state, config)

    # Flush events
    await _flush_events(result, req.project_id)

    # Now execute the product pipeline via agent factory
    await _run_department_pipeline(
        project_id=req.project_id,
        task_type="run_product_pipeline",
        description=f"Run full product pipeline for: {req.description}",
    )

    return {
        "project_id":    req.project_id,
        "status":        "requirements_in_progress",
        "current_phase": result.get("current_phase", 2),
        "phase_status":  result.get("phase_status", "running"),
        "message":       "Project started. Requirements pipeline running. Approval gate will trigger when ready.",
    }


@app.post("/projects/{project_id}/approve", tags=["Lifecycle"])
async def approve_artifact(
    project_id: str,
    req:        ApprovalRequest,
    db:         AsyncSession = Depends(get_db),
    user_id:    str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Injects an approval decision into the paused lifecycle graph.
    Graph resumes and advances to the next phase.
    """
    if not _lifecycle_graph:
        raise HTTPException(503, "Lifecycle graph not initialised")

    config = {"configurable": {"thread_id": project_id}}

    # Get current state to know which artifact type was awaiting approval
    state = _lifecycle_graph.get_state(config)
    if not state or not state.values:
        raise HTTPException(404, f"No active workflow for project {project_id}")

    current_state  = state.values
    artifact_type  = current_state.get("approval_artifact_type", "unknown")

    # Mark the most recent artifact of this type "approved" in the DB.
    # Genuine orchestration gap found while implementing M3.6: nothing
    # previously called ArtifactRepository.update_status here, so
    # AgentContextBuilder._fetch_artifacts (which strictly filters on
    # status == "approved") could never actually return the artifact
    # that was just approved to the next department's agent run. Most
    # visible for "deployment_plan": without this, DevOps's
    # execute_deployment stage would never be able to re-fetch its own
    # plan. Harmless no-op for "requirements"/"architecture" — those
    # are conceptual labels, not literal Artifact.artifact_type values,
    # so this lookup simply finds nothing for them, same as before.
    from sqlalchemy import select as _select
    from infrastructure.database.models import Artifact as _Artifact
    _latest = (await db.execute(
        _select(_Artifact)
        .where(_Artifact.project_id == project_id, _Artifact.artifact_type == artifact_type)
        .order_by(_Artifact.version.desc()).limit(1)
    )).scalar_one_or_none()
    if _latest is not None:
        await ArtifactRepository.update_status(db, _latest.id, "approved", approved_by=user_id)

    # Inject approval into graph state
    _lifecycle_graph.update_state(config, {
        "approval_status":   "approved",
        "approval_feedback": req.feedback or "Approved",
        "awaiting_approval": False,
        "revision_round":    0,  # reset on approval
    })

    # Resume graph execution
    result = _lifecycle_graph.invoke(None, config)
    await _flush_events(result, project_id)

    # Write audit event
    await AuditRepository.record(
        db,
        project_id=project_id,
        event_type=f"approval.granted.{artifact_type}",
        actor_type="user",
        actor_id=user_id,
        payload={"artifact_type": artifact_type, "feedback": req.feedback},
    )

    # If it's requirements approval, kick off architecture pipeline
    if artifact_type == "requirements":
        await _run_department_pipeline(
            project_id=project_id,
            task_type="run_architecture_pipeline",
            description="Run architecture pipeline after requirements approval",
        )

    # If it's architecture approval, kick off engineering pipeline (M3.3).
    # Engineering consumes the 5 architecture artifacts (including the
    # Appendix A ui_blueprint) — NOT "implementation_plan", which
    # EngineeringHead computes itself in-process from these inputs.
    elif artifact_type == "architecture":
        await _run_department_pipeline(
            project_id=project_id,
            task_type="run_engineering_pipeline",
            description="Run engineering pipeline after architecture approval",
        )

    # If it's deployment-plan approval, execute the actual deployment (M3.6).
    # Genuine orchestration gap discovered while implementing DevOps Service:
    # deployment_approval_gate_node/execute_deployment_phase_node in
    # lifecycle.py already pause-and-resume the graph correctly, but nothing
    # in this endpoint ever invoked devops_head a second time to run the
    # post-approval half of its pipeline (deploy -> health check -> release) —
    # the "requirements" and "architecture" branches above were the only two
    # wired up. Documented in docs/M3.6_DevOps_Service_Handover.md.
    elif artifact_type == "deployment_plan":
        await _run_department_pipeline(
            project_id=project_id,
            task_type="execute_deployment",
            description="Execute deployment after deployment plan approval",
        )

    return {
        "project_id":   project_id,
        "approved":     True,
        "artifact_type":artifact_type,
        "current_phase":result.get("current_phase", 3),
        "phase_status": result.get("phase_status", "running"),
    }


@app.post("/projects/{project_id}/reject", tags=["Lifecycle"])
async def reject_artifact(
    project_id: str,
    req:        RejectionRequest,
    db:         AsyncSession = Depends(get_db),
    user_id:    str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Injects a rejection decision. Graph routes back to the revision node.
    Feedback is injected into the department's next agent run.
    """
    if not _lifecycle_graph:
        raise HTTPException(503, "Lifecycle graph not initialised")

    config = {"configurable": {"thread_id": project_id}}
    state  = _lifecycle_graph.get_state(config)
    if not state or not state.values:
        raise HTTPException(404, f"No active workflow for project {project_id}")

    current_state = state.values
    artifact_type = current_state.get("approval_artifact_type", "unknown")

    _lifecycle_graph.update_state(config, {
        "approval_status":   "rejected",
        "approval_feedback": req.feedback,
        "awaiting_approval": False,
    })

    result = _lifecycle_graph.invoke(None, config)
    await _flush_events(result, project_id)

    await AuditRepository.record(
        db,
        project_id=project_id,
        event_type=f"approval.rejected.{artifact_type}",
        actor_type="user",
        actor_id=user_id,
        payload={"artifact_type": artifact_type, "feedback": req.feedback},
    )

    revision_round = result.get("revision_round", 1)
    return {
        "project_id":     project_id,
        "rejected":       True,
        "artifact_type":  artifact_type,
        "revision_round": revision_round,
        "message":        f"Revision {revision_round} started. Feedback forwarded to department.",
    }


@app.get("/projects/{project_id}/state", tags=["Lifecycle"])
async def get_project_state(
    project_id: str,
    user_id:    str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Returns the current lifecycle graph state for a project."""
    if not _lifecycle_graph:
        raise HTTPException(503, "Lifecycle graph not initialised")

    config = {"configurable": {"thread_id": project_id}}
    state  = _lifecycle_graph.get_state(config)

    if not state or not state.values:
        raise HTTPException(404, f"No active workflow for project {project_id}")

    vals = state.values
    return {
        "project_id":         project_id,
        "current_phase":      vals.get("current_phase", 0),
        "phase_status":       vals.get("phase_status", "unknown"),
        "awaiting_approval":  vals.get("awaiting_approval", False),
        "approval_artifact":  vals.get("approval_artifact_type"),
        "revision_round":     vals.get("revision_round", 0),
        "budget_status":      vals.get("budget_status", "active"),
        "total_spend_usd":    vals.get("total_spend_usd", 0.0),
        "artifacts":          vals.get("artifacts", {}),
        "failure_reason":     vals.get("failure_reason"),
    }


# ═══════════════════════════════════════════════════════════════
# DELEGATION — W12
# ═══════════════════════════════════════════════════════════════

@app.post("/delegate", tags=["Delegation"])
async def delegate_task(
    req:     DelegateTaskRequest,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """
    Dispatches a single task through W12 Task Delegation Graph.
    The graph selects the department, agent, and model, then executes.
    """
    if not _delegation_graph:
        raise HTTPException(503, "Delegation graph not initialised")

    task_id = str(uuid.uuid4())
    initial: DelegationState = {
        "project_id":      req.project_id,
        "task_id":         task_id,
        "task_type":       req.task_type,
        "task_description":req.description,
        "task_context":    {},
        "task_priority":   req.priority,
        "department":      None,
        "selected_agent":  None,
        "selected_provider":None,
        "selected_model":  None,
        "agent_run_id":    None,
        "task_status":     "pending",
        "task_output":     None,
        "validation_passed":False,
        "retry_count":     0,
        "max_retries":     req.max_retries,
        "escalation_level":0,
        "dead_lettered":   False,
        "failure_reason":  None,
    }

    config = {"configurable": {"thread_id": f"{req.project_id}_{task_id}"}}
    result = _delegation_graph.invoke(initial, config)

    return {
        "task_id":         task_id,
        "task_status":     result.get("task_status", "unknown"),
        "department":      result.get("department"),
        "selected_agent":  result.get("selected_agent"),
        "selected_model":  result.get("selected_model"),
        "validation_passed":result.get("validation_passed", False),
        "dead_lettered":   result.get("dead_lettered", False),
        "failure_reason":  result.get("failure_reason"),
    }


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/{project_id}")
async def ws_endpoint(project_id: str, websocket: WebSocket):
    ws_connections.inc()
    try:
        await ws_manager.serve(project_id, websocket)
    finally:
        ws_connections.dec()


# ═══════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════

async def _flush_events(state: Dict[str, Any], project_id: str) -> None:
    """Flushes NATS and WebSocket events queued during graph execution."""
    nats = _get_nats_safe()
    for event in state.get("nats_events_queue", []):
        try:
            if nats:
                await nats.publish(event["subject"], event["payload"])
        except Exception as e:
            log.warning("nats_flush_failed", error=str(e))

    for event in state.get("websocket_events_queue", []):
        try:
            await ws_manager.broadcast(
                project_id=event.get("project_id", project_id),
                event_type=event.get("event_type", "update"),
                payload=event.get("payload", {}),
            )
        except Exception as e:
            log.warning("ws_flush_failed", error=str(e))


async def _run_department_pipeline(
    project_id:  str,
    task_type:   str,
    description: str,
) -> None:
    """
    Builds context and runs the department head agent synchronously.
    In Phase 3 this will be replaced by async NATS-driven execution.
    """
    if not _agent_factory or not _context_builder:
        return

    # Each department consumes different upstream artifact types.
    # NOTE: this was previously hardcoded to the Product-only list, which
    # happened to be harmless for Architecture (it re-derives from the
    # same Product artifacts) but would have silently starved Engineering
    # of every architecture artifact — including the Appendix A
    # ui_blueprint — since "implementation_plan" isn't a stored artifact
    # type at all (EngineeringHead computes it in-process from these).
    DEPARTMENT_ARTIFACT_TYPES: Dict[str, list] = {
        "product":      ["feature_spec_doc", "requirements_doc",
                          "user_stories_doc", "acceptance_criteria"],
        "architecture":  ["feature_spec_doc", "requirements_doc",
                          "user_stories_doc", "acceptance_criteria"],
        "engineering":   ["architecture_blueprint", "openapi_spec", "database_schema",
                          "deployment_architecture", "ui_blueprint"],
        # M3.9 — genuine orchestration gap, same class of bug as the devops
        # entry below: services/qa/head/__init__.py and
        # services/security/head/__init__.py both declare
        # REQUIRED_ENGINEERING_ARTIFACTS = ("source_code",) and read it via
        # task.context.get_artifact("source_code", {}) — but with no "qa"/
        # "security" entry here, a Manager-delegated QA or Security task
        # fell back to the "product" default list (feature_spec_doc etc.)
        # and never received source_code at all. See
        # docs/M3.9_Platform_Integration_Handover.md.
        "qa":            ["source_code"],
        "security":      ["source_code"],
        # M3.6 — genuine orchestration gap: without this entry, devops_head's
        # execute_deployment stage would silently fall back to the "product"
        # default list above and never see qa_report/security_report/
        # deployment_plan at all. See docs/M3.6_DevOps_Service_Handover.md.
        "devops":        ["source_code", "qa_report", "security_report", "deployment_plan",
                           "dockerfile", "docker_compose", "environment_config",
                           "pipeline_config", "openapi_spec", "database_schema"],
    }

    try:
        # Determine which agent to run
        from services.manager.graphs.delegation import DEPARTMENT_HEAD_MAP, TASK_DEPARTMENT_MAP
        dept     = TASK_DEPARTMENT_MAP.get(task_type, "product")
        agent_id = DEPARTMENT_HEAD_MAP.get(dept, "product_head")

        # Build context
        ctx = await _context_builder.build(
            project_id=project_id,
            task_type=task_type,
            agent_id=agent_id,
            artifact_types=DEPARTMENT_ARTIFACT_TYPES.get(
                dept, ["feature_spec_doc", "requirements_doc",
                       "user_stories_doc", "acceptance_criteria"],
            ),
        )
        # Inject factory reference so head agents can create workers
        ctx.approved_artifacts["__factory__"] = _agent_factory

        task = TaskInput.create(
            project_id=project_id,
            agent_id=agent_id,
            parent_agent_id="manager_agent",
            task_type=task_type,
            description=description,
            expected_output="Completed department pipeline artifacts",
            context=ctx,
        )

        agent  = _agent_factory.create(agent_id)
        result = await agent.run(task)

        log.info("department_pipeline_done",
                 dept=dept, status=result.status.value,
                 artifacts=len(result.artifacts))

        # Broadcast completion to WebSocket
        await ws_manager.broadcast(
            project_id=project_id,
            event_type=f"{dept}.pipeline.completed",
            payload={
                "status":    result.status.value,
                "artifacts": len(result.artifacts),
                "summary":   result.summary,
            },
        )

    except Exception as e:
        log.error("department_pipeline_error", task_type=task_type, error=str(e), exc_info=True)
        await ws_manager.broadcast(
            project_id=project_id,
            event_type="pipeline.error",
            payload={"task_type": task_type, "error": str(e)},
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("services.manager.main:app",
                host=settings.app_host, port=8001,
                reload=settings.is_development, log_config=None)
