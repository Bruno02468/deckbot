from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
  AsyncEngine,
  AsyncSession,
  async_sessionmaker,
  create_async_engine,
)

from deckbot.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
  global _engine
  if _engine is None:
    settings = get_settings()
    _engine = create_async_engine(
      f"sqlite+aiosqlite:///{settings.db_path}",
      echo=False,
    )
  return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
  global _session_factory
  if _session_factory is None:
    _session_factory = async_sessionmaker(
      get_engine(),
      class_=AsyncSession,
      expire_on_commit=False,
    )
  return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
  factory = get_session_factory()
  async with factory() as session:
    yield session


async def enable_wal() -> None:
  """Enable WAL mode and foreign-key enforcement on the SQLite DB."""
  async with get_engine().connect() as conn:
    await conn.execute(text("PRAGMA journal_mode=WAL"))
    await conn.execute(text("PRAGMA foreign_keys=ON"))
    await conn.commit()
