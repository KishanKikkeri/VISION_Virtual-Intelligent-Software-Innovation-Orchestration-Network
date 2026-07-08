"""
services/repository/main.py
==============================
Repository Service — the central Git abstraction layer.
Engineering agents never run Git directly; everything goes through
the HTTP API mounted here.

Run standalone:
    uvicorn services.repository.main:app --host 0.0.0.0 --port 8006
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config.settings import get_settings
from infrastructure.database.connection import close_db, get_db_context, init_db
from infrastructure.messaging.nats_client import get_nats, init_nats
from infrastructure.monitoring.telemetry import configure_telemetry
from infrastructure.secrets.validator import validate_secrets
from services.repository.api.events import setup_repository_subscriptions
from services.repository.api.routes import router, set_deps
from services.repository.managers import RepositoryDeps
from services.repository.providers.github_provider import GitHubProvider

log = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_secrets(exit_on_failure=False)
    configure_telemetry(metrics_port=9106)
    await init_db()

    provider = GitHubProvider(
        token=settings.github_token or "",
        base_url=settings.github_api_base_url,
    )

    nats_client = None
    try:
        nats_client = await init_nats(settings.nats_url)
    except Exception as e:
        log.warning("nats_unavailable", error=str(e))

    deps = RepositoryDeps(
        db_factory=get_db_context,
        provider=provider,
        nats=nats_client,
        default_owner=settings.github_default_owner,
    )
    set_deps(deps)

    if nats_client is not None:
        try:
            await setup_repository_subscriptions(nats_client, deps)
        except Exception as e:
            log.warning("repository_subscriptions_setup_failed", error=str(e))

    log.info("repository_service_ready")
    yield

    try:
        if nats_client is not None:
            await nats_client.drain()
    except Exception:
        pass
    await close_db()


app = FastAPI(title="AASC Repository Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.repository.main:app",
        host=settings.app_host,
        port=settings.repository_service_port,
        reload=settings.is_development,
    )
