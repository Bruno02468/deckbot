from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.db.session import get_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
  """FastAPI dependency that yields an async DB session."""
  async with get_session_factory()() as session:
    yield session
