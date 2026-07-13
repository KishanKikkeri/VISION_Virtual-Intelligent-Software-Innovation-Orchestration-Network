"""
services/monitoring/main.py
================================
Monitoring Service entrypoint — the platform's first continuous-running
department (spec §7). Unlike every other service's main.py, this one
is NOT optional/standalone-parity — the scheduler background task
started here IS the mechanism that makes "continuous monitoring" real.

Run:
    uvicorn services.monitoring.main:app --host 0.0.0.0 --port 8011
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config.settings import get_settings
from core.runtime.factory import AgentFactory
from infrastructure.database.connection import close_db, get_db_context, init_db
from infrastructure.database.repositories import (
    ArtifactRepository, AuditRepository, TokenLedgerRepository,
)
from infrastructure.messaging.nats_client import init_nats
from infrastructure.monitoring.telemetry import configure_telemetry
from infrastructure.storage.base import get_storage, init_storage
from services.monitoring.api.events import setup_monitoring_subscriptions
from services.monitoring.api.routes import router
from services.monitoring.integration.platform_anchor import ensure_platform_anchor
from services.monitoring.workflows.monitoring_graph import build_monitoring_graph, initial_state

log = structlog.get_logger(__name__)
settings = get_settings()

_scheduler_task = None
_shutting_down = False


async def _monitoring_loop(factory: AgentFactory, project_id: str) -> None:
    """
    The continuous-execution mechanism (spec §0 Decision 1 / §7): a
    bounded W-MONITORING cycle, re-invoked on a fixed interval, with a
    STABLE thread_id so rolling state (consecutive_critical_count,
    last_alert_at, capacity trend windows) persists across cycles via
    the graph's checkpointer. This loop itself is the only "while" in
    the entire codebase's continuous-department story — no LangGraph
    node loops internally (see monitoring_graph.py's module docstring).
    """
    graph = build_monitoring_graph(factory)
    state = initial_state(
        project_id=project_id,
        cycle_interval_seconds=settings.monitoring_cycle_interval_seconds,
        incident_breach_cycles=settings.monitoring_incident_breach_cycles,
        alert_dedup_seconds=settings.monitoring_alert_dedup_seconds,
    )
    config = {"configurable": {"thread_id": "monitoring-continuous"}}

    while not _shutting_down:
        try:
            state = await graph.ainvoke(state, config=config)
            log.info("monitoring_cycle_completed", cycle=state.get("cycle_count"),
                     health_score=state.get("health_score"), status=state.get("status"))
        except Exception as e:
            log.error("monitoring_cycle_failed", error=str(e), exc_info=True)
        await asyncio.sleep(settings.monitoring_cycle_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task, _shutting_down
    _shutting_down = False

    configure_telemetry(metrics_port=9111)
    await init_db()

    nats_client = None
    try:
        nats_client = await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    init_storage(backend=settings.storage_backend, base_path=settings.storage_local_path)

    if nats_client is not None:
        try:
            await setup_monitoring_subscriptions(nats_client, db_factory=get_db_context)
        except Exception as e:
            log.warning("monitoring_subscriptions_setup_failed", error=str(e))

    project_id = None
    try:
        project_id = await ensure_platform_anchor(get_db_context)
    except Exception as e:
        log.error("platform_anchor_failed", error=str(e))

    factory = AgentFactory(
        db_factory=get_db_context,
        nats=nats_client,
        storage=get_storage(),
        audit_repo=AuditRepository,
        artifact_repo=ArtifactRepository,
        token_repo=TokenLedgerRepository,
    )

    if project_id is not None:
        _scheduler_task = asyncio.create_task(_monitoring_loop(factory, project_id))
    else:
        log.error("monitoring_scheduler_not_started", reason="platform anchor unavailable")

    log.info("monitoring_service_ready")
    yield

    _shutting_down = True
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass

    try:
        if nats_client is not None:
            await nats_client.drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC Monitoring Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.monitoring.main:app",
        host=settings.app_host,
        port=settings.monitoring_service_port,
        reload=settings.is_development,
    )
