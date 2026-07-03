"""
infrastructure/database/connection.py
=======================================
Sprint 3 — Database Module.
Provides the async SQLAlchemy engine, session factory, and Base model class.
All database access in AASC goes through get_db() dependency injection.
Direct engine usage is only permitted in migrations (Alembic) and scripts.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config.settings import get_settings

log = structlog.get_logger(__name__)

# Module-level singletons — set once by init_db()
_engine:          AsyncEngine           | None = None
_session_factory: async_sessionmaker    | None = None


class Base(DeclarativeBase):
    """
    SQLAlchemy declarative base for all AASC ORM models.
    All models in infrastructure/database/models.py extend this.
    """
    pass


async def init_db() -> None:
    """
    Initialises the async database engine and session factory.
    Called once during FastAPI startup lifespan.
    """
    global _engine, _session_factory
    settings = get_settings()

    _engine = create_async_engine(
        settings.database_url,
        echo=settings.is_development,         # SQL logging in dev only
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,                   # verify connection on checkout
        pool_recycle=3600,                    # recycle connections every hour
        connect_args={
            "server_settings": {
                "application_name": "aasc",
            }
        },
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,               # safer for async patterns
        autoflush=False,
        autocommit=False,
    )

    # Verify connectivity
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    log.info("database_connected", url=settings.database_url.split("@")[-1])


async def close_db() -> None:
    """Disposes the engine on application shutdown."""
    global _engine
    if _engine:
        await _engine.dispose()
        log.info("database_disconnected")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency. Yields a database session per request.
    Automatically commits on success, rolls back on exception.

    Usage in a route:
        async def create_project(db: AsyncSession = Depends(get_db)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager version for use outside FastAPI route handlers.
    Used in background tasks, scripts, and LangGraph nodes.

    Usage:
        async with get_db_context() as db:
            await project_repo.create(db, ...)
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db_health() -> bool:
    """Returns True if the database is reachable. Used in /health endpoint."""
    try:
        if _engine is None:
            return False
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
