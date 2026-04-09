import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from deckbot.config import get_settings
from deckbot.db.models import Base

alembic_config = context.config

if alembic_config.config_file_name is not None:
  fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata


def get_db_url() -> str:
  settings = get_settings()
  return f"sqlite+aiosqlite:///{settings.db_path}"


def run_migrations_offline() -> None:
  context.configure(
    url=get_db_url(),
    target_metadata=target_metadata,
    literal_binds=True,
    dialect_opts={"paramstyle": "named"},
    render_as_batch=True,
  )
  with context.begin_transaction():
    context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
  context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_as_batch=True,
  )
  with context.begin_transaction():
    context.run_migrations()


async def run_async_migrations() -> None:
  configuration = alembic_config.get_section(
    alembic_config.config_ini_section, {}
  )
  configuration["sqlalchemy.url"] = get_db_url()
  connectable = async_engine_from_config(
    configuration,
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
