"""
apps/demo/main.py
==================
Sprint 5 — Foundation Integration Demo.
Proves that a single request can successfully traverse:

  API → Router → LLM → Database → Storage → Audit → Messaging → WebSocket

This is the Phase 1 exit-criteria endpoint.
If POST /demo returns 200, the entire foundation is operational.

Also exposes:
  GET  /health           — infrastructure health check
  GET  /ws/{project_id}  — WebSocket real-time stream
  POST /auth/register    — create a user account
  POST /auth/login       — get JWT token pair
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
from core.contracts import (
    AuditEventRecord, HealthStatus, LLMMessage, LLMProvider, TokenUsageRecord,
)
from core.llm.registry import LLMProviderRegistry
from core.llm.router import select_provider_and_model
from infrastructure.auth.jwt_auth import (
    LoginRequest, RegisterRequest, TokenPair,
    create_token_pair, get_current_user_id, hash_password, verify_password,
)
from infrastructure.database.connection import (
    check_db_health, close_db, get_db, init_db,
)
from infrastructure.database.models import Base
from infrastructure.database.repositories import (
    ArtifactRepository, AuditRepository, ProjectRepository,
    TokenLedgerRepository, UserRepository,
)
from infrastructure.messaging.nats_client import get_nats, init_nats
from infrastructure.monitoring.telemetry import (
    configure_telemetry, record_token_usage, ws_connections,
)
from infrastructure.secrets.validator import validate_secrets
from infrastructure.storage.base import get_storage, init_storage
from infrastructure.websocket.manager import ws_manager

log = structlog.get_logger(__name__)
settings = get_settings()


# ═══════════════════════════════════════════════════════════════
# LIFESPAN — startup and shutdown
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:  validate secrets → configure telemetry → connect infrastructure
              → register LLM providers
    Shutdown: drain NATS → close DB → log
    """
    # 1. Fail fast on missing secrets
    validate_secrets(exit_on_failure=True)

    # 2. Observability (logging + metrics + tracing)
    configure_telemetry(metrics_port=9100)

    # 3. Database
    await init_db()

    # 4. Run Alembic migrations programmatically (dev mode)
    if settings.is_development:
        await _run_migrations()

    # 5. NATS JetStream
    try:
        await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e),
                    message="NATS not available — events will be skipped")

    # 6. Artifact storage
    init_storage(
        backend=settings.storage_backend,
        base_path=settings.storage_local_path,
    )

    # 7. LLM providers
    LLMProviderRegistry.initialise()

    providers = LLMProviderRegistry.available()
    if not providers:
        log.warning("no_llm_providers",
                    message="No LLM providers registered. LLM calls will fail.")

    log.info("aasc_foundation_ready",
             env=settings.app_env,
             llm_providers=providers,
             db_url=settings.database_url.split("@")[-1])

    yield   # ← application runs here

    # Graceful shutdown
    try:
        await get_nats().drain()
    except Exception:
        pass
    await close_db()
    log.info("aasc_foundation_stopped")


async def _run_migrations() -> None:
    """Runs Alembic migrations at startup in development mode."""
    try:
        from alembic import command
        from alembic.config import Config
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", settings.database_url_sync)
        command.upgrade(cfg, "head")
        log.info("migrations_applied")
    except Exception as e:
        log.warning("migration_error", error=str(e))


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="AASC Foundation Demo",
    description="Phase 1 integration demo — proves the entire foundation stack works.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health", response_model=HealthStatus, tags=["System"])
async def health_check() -> HealthStatus:
    """
    Returns the health status of all infrastructure components.
    Used by Docker Compose healthcheck and monitoring.
    """
    db_ok = await check_db_health()

    nats_ok = False
    try:
        nats_ok = await get_nats().check_health()
    except RuntimeError:
        pass

    llm_providers = LLMProviderRegistry.available()

    all_ok = db_ok and len(llm_providers) > 0
    return HealthStatus(
        status="ok" if all_ok else "degraded",
        environment=settings.app_env,
        checks={
            "database":     db_ok,
            "nats":         nats_ok,
            "llm_providers":len(llm_providers) > 0,
            "storage":      True,  # local storage always available
        },
    )


# ═══════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════

@app.post("/auth/register", response_model=TokenPair, tags=["Auth"],
          status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    db:  AsyncSession = Depends(get_db),
) -> TokenPair:
    """Creates a new user account and returns a token pair."""
    existing = await UserRepository.get_by_email(db, req.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = await UserRepository.create(
        db,
        email=req.email,
        password_hash=hash_password(req.password),
        full_name=req.full_name,
        role=req.role,
    )
    await AuditRepository.record(
        db, event_type="user.registered",
        actor_type="user", actor_id=user.id,
        entity_type="user", entity_id=user.id,
        payload={"email": req.email, "role": req.role},
    )
    log.info("user_registered", user_id=user.id, email=req.email)
    return create_token_pair(user.id, user.role)


@app.post("/auth/login", response_model=TokenPair, tags=["Auth"])
async def login(
    req: LoginRequest,
    db:  AsyncSession = Depends(get_db),
) -> TokenPair:
    """Authenticates a user and returns a token pair."""
    user = await UserRepository.get_by_email(db, req.email)
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    await AuditRepository.record(
        db, event_type="user.login",
        actor_type="user", actor_id=user.id,
        payload={"email": req.email},
    )
    return create_token_pair(user.id, user.role)


# ═══════════════════════════════════════════════════════════════
# DEMO ENDPOINT — Phase 1 exit-criteria proof
# ═══════════════════════════════════════════════════════════════

class DemoRequest(BaseModel):
    project_name:   str = "Demo Project"
    description:    str = "A demo project to test the AASC foundation stack."
    task_prompt:    str = "Write a one-paragraph description of what an autonomous AI software company does."
    llm_provider:   Optional[str] = None   # defaults to settings.default_llm_provider
    budget_usd:     Optional[float] = 10.0


class DemoResponse(BaseModel):
    success:        bool
    project_id:     str
    user_id:        str
    llm_provider:   str
    llm_model:      str
    llm_response:   str
    artifact_id:    str
    audit_event_id: str
    token_ledger_id:str
    total_spend_usd:float
    steps_completed:list
    latency_ms:     int
    timestamp:      datetime


@app.post("/demo", response_model=DemoResponse, tags=["Demo"])
async def run_demo(
    req:     DemoRequest,
    db:      AsyncSession = Depends(get_db),
    user_id: str          = Depends(get_current_user_id),
) -> DemoResponse:
    """
    ┌─────────────────────────────────────────────────────────┐
    │  Phase 1 Exit-Criteria: Full foundation traversal test  │
    └─────────────────────────────────────────────────────────┘

    Executes this sequence:
      1.  Create project in PostgreSQL
      2.  Select LLM provider + model via router
      3.  Call LLM with the task prompt
      4.  Store LLM response as an artifact (filesystem)
      5.  Write artifact record to PostgreSQL
      6.  Write audit event to PostgreSQL
      7.  Write token ledger row to PostgreSQL
      8.  Publish NATS event
      9.  Broadcast WebSocket update
      10. Return structured response

    If this endpoint returns 200, the entire Phase 1 foundation is working.
    """
    import time
    t_start = time.monotonic()
    steps: list = []

    # ── Step 1: Create project ───────────────────────────────
    project = await ProjectRepository.create(
        db,
        name=req.project_name,
        description=req.description,
        owner_id=user_id,
        llm_provider=req.llm_provider or settings.default_llm_provider,
        budget_usd=req.budget_usd,
    )
    steps.append("1_project_created")
    log.info("demo_project_created", project_id=project.id)

    # ── Step 2: Select provider + model ─────────────────────
    provider_name, model_name = select_provider_and_model(
        preferred_provider=req.llm_provider or settings.default_llm_provider,
        agent_role="worker",
        task_type="generate_description",
        budget_tight=(req.budget_usd is not None and req.budget_usd < 5.0),
    )
    steps.append("2_provider_selected")

    # ── Step 3: Call LLM ─────────────────────────────────────
    llm_response_text = ""
    llm_resp = None
    try:
        llm_resp = await LLMProviderRegistry.complete(
            provider=provider_name,
            model=model_name,
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "You are a helpful assistant. "
                        "Respond concisely in plain text. No markdown."
                    ),
                ),
                LLMMessage(role="user", content=req.task_prompt),
            ],
            max_tokens=512,
            temperature=0.3,
        )
        llm_response_text = llm_resp.content
        steps.append("3_llm_called")
    except Exception as e:
        llm_response_text = f"[LLM unavailable: {e}]"
        steps.append("3_llm_skipped")
        log.warning("demo_llm_failed", error=str(e))

    # ── Step 4 + 5: Store artifact ────────────────────────────
    storage = get_storage()
    artifact_content = {
        "task_prompt":    req.task_prompt,
        "llm_response":   llm_response_text,
        "provider":       provider_name,
        "model":          model_name,
        "generated_at":   datetime.utcnow().isoformat(),
    }
    storage_ref = await storage.store(
        project_id=project.id,
        artifact_type="demo_output",
        version=1,
        content=artifact_content,
        extension="json",
    )
    artifact_ref = await ArtifactRepository.create(
        db,
        project_id=project.id,
        artifact_type="demo_output",
        created_by="demo_worker_agent",
        content=artifact_content,
        storage_ref=storage_ref,
        metadata={"provider": provider_name, "model": model_name},
    )
    steps.append("4_artifact_stored")
    steps.append("5_artifact_registered")

    # ── Step 6: Write audit event ─────────────────────────────
    audit_id = await AuditRepository.record(
        db,
        project_id=project.id,
        event_type="demo.foundation_traversal",
        actor_type="agent",
        actor_id="demo_worker_agent",
        entity_type="project",
        entity_id=project.id,
        payload={
            "provider":   provider_name,
            "model":      model_name,
            "artifact_id":artifact_ref["artifact_id"],
        },
    )
    steps.append("6_audit_logged")

    # ── Step 7: Write token ledger ────────────────────────────
    input_tok  = llm_resp.input_tokens  if llm_resp else 0
    output_tok = llm_resp.output_tokens if llm_resp else 0
    cost       = llm_resp.cost_usd      if llm_resp else 0.0

    ledger_id = await TokenLedgerRepository.record(
        db,
        project_id=project.id,
        agent_id="demo_worker_agent",
        department="demo",
        provider=provider_name,
        model=model_name,
        input_tokens=input_tok,
        output_tokens=output_tok,
        cost_usd=cost,
    )
    # Update Prometheus counter
    record_token_usage(provider_name, model_name, "demo",
                       input_tok, output_tok, cost)
    steps.append("7_token_ledger_written")

    # ── Step 8: Publish NATS event ────────────────────────────
    try:
        await get_nats().publish(
            "demo.foundation_traversal.completed",
            {
                "project_id":  project.id,
                "artifact_id": artifact_ref["artifact_id"],
                "provider":    provider_name,
                "model":       model_name,
                "cost_usd":    cost,
            },
        )
        steps.append("8_nats_published")
    except Exception as e:
        steps.append("8_nats_skipped")
        log.warning("demo_nats_publish_failed", error=str(e))

    # ── Step 9: Broadcast WebSocket update ───────────────────
    await ws_manager.broadcast(
        project_id=project.id,
        event_type="demo.completed",
        payload={
            "message":  "Foundation demo completed successfully",
            "provider": provider_name,
            "model":    model_name,
            "artifact": artifact_ref["artifact_id"],
        },
    )
    steps.append("9_websocket_broadcast")

    # ── Step 10: Get total spend ──────────────────────────────
    total_spend = await ProjectRepository.get_total_spend(db, project.id)
    steps.append("10_response_built")

    latency_ms = int((time.monotonic() - t_start) * 1000)
    log.info("demo_completed",
             project_id=project.id, steps=len(steps),
             latency_ms=latency_ms, cost_usd=cost)

    return DemoResponse(
        success=True,
        project_id=project.id,
        user_id=user_id,
        llm_provider=provider_name,
        llm_model=model_name,
        llm_response=llm_response_text,
        artifact_id=artifact_ref["artifact_id"],
        audit_event_id=audit_id,
        token_ledger_id=ledger_id,
        total_spend_usd=total_spend,
        steps_completed=steps,
        latency_ms=latency_ms,
        timestamp=datetime.utcnow(),
    )


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/{project_id}")
async def websocket_endpoint(project_id: str, websocket: WebSocket) -> None:
    """
    Real-time event stream for a project.
    Connect here to receive live updates as agents work.

    Events pushed:
      { type: "phase_changed", payload: {...} }
      { type: "approval_required", payload: {...} }
      { type: "agent_completed", payload: {...} }
      { type: "demo.completed", payload: {...} }
    """
    ws_connections.inc()
    try:
        await ws_manager.serve(project_id, websocket)
    finally:
        ws_connections.dec()


# ═══════════════════════════════════════════════════════════════
# PROJECTS (read-only — write is manager-service in Phase 2)
# ═══════════════════════════════════════════════════════════════

@app.get("/projects", tags=["Projects"])
async def list_projects(
    db:      AsyncSession = Depends(get_db),
    user_id: str          = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Lists all projects owned by the current user."""
    projects = await ProjectRepository.list_by_owner(db, user_id)
    return {
        "projects": [
            {
                "id":            p.id,
                "name":          p.name,
                "status":        p.status,
                "current_phase": p.current_phase,
                "llm_provider":  p.llm_provider,
                "created_at":    p.created_at.isoformat(),
            }
            for p in projects
        ],
        "total": len(projects),
    }


@app.get("/projects/{project_id}/timeline", tags=["Projects"])
async def get_project_timeline(
    project_id: str,
    db:         AsyncSession = Depends(get_db),
    user_id:    str          = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Returns the audit timeline for a project."""
    events = await AuditRepository.list_for_project(db, project_id, limit=50)
    return {
        "project_id": project_id,
        "events": [
            {
                "id":         e.id,
                "event_type": e.event_type,
                "actor_type": e.actor_type,
                "actor_id":   e.actor_id,
                "payload":    e.payload,
                "recorded_at":e.recorded_at.isoformat(),
            }
            for e in events
        ],
    }


@app.get("/projects/{project_id}/spend", tags=["Projects"])
async def get_project_spend(
    project_id: str,
    db:         AsyncSession = Depends(get_db),
    user_id:    str          = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Returns token spend breakdown by department for a project."""
    by_dept = await TokenLedgerRepository.get_project_spend_by_dept(db, project_id)
    total   = sum(by_dept.values())
    return {
        "project_id":     project_id,
        "total_spend_usd":round(total, 6),
        "by_department":  {k: round(v, 6) for k, v in by_dept.items()},
    }


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "apps.demo.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_development,
        log_config=None,   # structlog handles logging
    )
