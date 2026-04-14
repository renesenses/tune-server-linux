"""Alembic environment — Tune Server database migrations.

Reads database URL from TUNE_DB_URL or defaults to SQLite.
Uses tables.py metadata as the single source of truth.
"""
import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from tune_server.db.tables import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build SA URL from environment
_db_engine = os.environ.get("TUNE_DB_ENGINE", "sqlite")
_db_path = os.environ.get("TUNE_DB_PATH", "tune_server.db")
_db_url = os.environ.get("TUNE_DB_URL")

if _db_engine == "sqlite":
    _sa_url = f"sqlite+aiosqlite:///{_db_path}"
elif _db_url:
    _sa_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    _sa_url = f"sqlite+aiosqlite:///{_db_path}"

config.set_main_option("sqlalchemy.url", _sa_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
