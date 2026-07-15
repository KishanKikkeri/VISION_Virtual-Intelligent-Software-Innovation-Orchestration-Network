"""
services/incident_response/main.py
================================
Incident Response Service entrypoint — event-driven, not scheduled
(spec §10 "Scheduler belongs only to Monitoring"). Unlike
services/monitoring/main.py, there is no background loop here: the
NATS subscription set up in api/events.py IS the mechanism that starts
each incident's W-INCIDENT-RESPONSE lifecycle, one `monitoring.incident`
message at a time.

Run:
    uvicorn services.incident_response.main:app --host 0.0.0.0 --port 8012
"""
from __future__ import annotations

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
from services.incident_response.api.events import setup_incident_response_subscriptions
from services.incident_response.api.routes import router
from services.incident_response.integration.platform_anchor import ensure_platform_anchor

log = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_telemetry(metrics_port=9112)
    await init_db()

    nats_client = None
    try:
        nats_client = await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    init_storage(backend=settings.storage_backend, base_path=settings.storage_local_path)

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

    if nats_client is not None and project_id is not None:
        try:
            await setup_incident_response_subscriptions(
                nats_client, factory, project_id, db_factory=get_db_context)
        except Exception as e:
            log.warning("incident_response_subscriptions_setup_failed", error=str(e))
    else:
        log.error("incident_response_subscriptions_not_started",
                  reason="nats or platform anchor unavailable")

    log.info("incident_response_service_ready")
    yield

    try:
        if nats_client is not None:
            await nats_client.drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC Incident Response Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.incident_response.main:app",
        host=settings.app_host,
        port=settings.incident_response_service_port,
        reload=settings.is_development,
    )
