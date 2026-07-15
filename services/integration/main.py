"""
services/integration/main.py
=================================
Platform Integration Service entrypoint. Serves the 7 /platform/*
APIs and runs startup validation + one initial full validation pass
on boot.

Run:
    uvicorn services.integration.main:app --host 0.0.0.0 --port 8013
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config.settings import get_settings
from core.runtime.factory import AgentFactory
from infrastructure.database.connection import close_db, get_db_context, init_db
from infrastructure.database.repositories import ArtifactRepository, AuditRepository, TokenLedgerRepository
from infrastructure.messaging.nats_client import init_nats
from infrastructure.monitoring.telemetry import configure_telemetry
from infrastructure.storage.base import get_storage, init_storage
from services.integration.api.events import run_validation_and_publish, setup_platform_visibility_subscriptions
from services.integration.api.routes import router
from services.integration.startup import run_startup_checks

log = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_telemetry(metrics_port=9113)
    await init_db()

    nats_client = None
    try:
        nats_client = await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    init_storage(backend=settings.storage_backend, base_path=settings.storage_local_path)

    factory = AgentFactory(
        db_factory=get_db_context, nats=nats_client, storage=get_storage(),
        audit_repo=AuditRepository, artifact_repo=ArtifactRepository, token_repo=TokenLedgerRepository,
    )

    app.state.db_factory = get_db_context
    app.state.nats = nats_client
    app.state.agent_factory = factory

    # Startup validation is non-strict here — this service's whole job
    # is to report on platform health, including when the platform
    # itself is unhealthy, so it must still come up and serve
    # /platform/report in that case (spec §5's "fail fast" applies to
    # the *platform*'s own startup narrative, not to this reporting
    # service refusing to exist).
    startup_report = await run_startup_checks(db_factory=get_db_context, strict=False)
    if not startup_report.passed:
        log.warning("platform_startup_checks_incomplete",
                    failed=[c.name for c in startup_report.checks if not c.passed])
    else:
        log.info("platform_startup_checks_passed")

    if nats_client is not None:
        try:
            await setup_platform_visibility_subscriptions(nats_client)
            await run_validation_and_publish(nats_client, db_factory=get_db_context, factory=factory)
        except Exception as e:
            log.warning("platform_integration_startup_validation_failed", error=str(e))

    log.info("platform_integration_service_ready")
    yield

    try:
        if nats_client is not None:
            await nats_client.drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC Platform Integration Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.integration.main:app",
        host=settings.app_host, port=settings.integration_service_port,
        reload=settings.is_development,
    )
