from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from deckbot.api.routers import files, jobs, nodes
from deckbot.db.session import enable_wal


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
  await enable_wal()
  yield


def create_app() -> FastAPI:
  app = FastAPI(
    title="DeckBot API",
    version="1.0",
    description="Internal API for MYSTRAN run nodes.",
    lifespan=_lifespan,
  )
  app.include_router(nodes.router, prefix="/api/v1")
  app.include_router(jobs.router, prefix="/api/v1")
  app.include_router(files.router)
  return app


# Module-level instance so uvicorn can reference "deckbot.api.app:app".
app = create_app()
