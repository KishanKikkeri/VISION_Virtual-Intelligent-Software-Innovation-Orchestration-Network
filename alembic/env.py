"""Alembic migrations environment."""
from __future__ import annotations
import asyncio
from logging.config import fileConfig
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from core.config.settings import get_settings
from infrastructure.database.connection import Base
from infrastructure.database.models import *  # ensure all models are registered

config  = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
settings = get_settings()

def run_migrations_offline():
    url = settings.database_url_sync
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle":"named"})
    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online():
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: context.configure(
            connection=c, target_metadata=target_metadata))
        await conn.run_sync(lambda _: context.run_migrations())
    await engine.dispose()

if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
