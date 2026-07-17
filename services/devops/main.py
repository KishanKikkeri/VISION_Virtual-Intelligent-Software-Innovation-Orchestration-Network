"""
services/devops/main.py
============================
DevOps Service standalone entrypoint (optional — DevOps is normally
invoked in-process by Manager Service via AgentFactory, the same way
Engineering, QA, and Security are). Provided for parity and ops/testing
convenience.

Run standalone:
    uvicorn services.devops.main:app --host 0.0.0.0 --port 8010
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config.settings import get_settings
from infrastructure.database.connection import close_db, init_db
from infrastructure.messaging.nats_client import init_nats
from infrastructure.monitoring.telemetry import configure_telemetry
from services.devops.api.events import setup_devops_subscriptions
from services.devops.api.routes import router

log = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_telemetry(metrics_port=9110)
    await init_db()

    nats_client = None
    try:
        nats_client = await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    if nats_client is not None:
        try:
            await setup_devops_subscriptions(nats_client, runner=None)
        except Exception as e:
            log.warning("devops_subscriptions_setup_failed", error=str(e))

    log.info("devops_service_ready")
    yield

    try:
        if nats_client is not None:
            await nats_client.drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC DevOps Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.devops.main:app",
        host=settings.app_host,
        port=settings.devops_service_port,
        reload=settings.is_development,
    )
