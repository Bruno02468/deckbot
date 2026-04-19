from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.api.auth import require_active_node
from deckbot.api.deps import get_db_session
from deckbot.db.models import Node

router = APIRouter(tags=["nodes"])


class KeepaliveBody(BaseModel):
  max_threads: int | None = None


@router.post("/keepalive", status_code=204)
async def keepalive(
  body: KeepaliveBody,
  node: Node = Depends(require_active_node),
  session: AsyncSession = Depends(get_db_session),
) -> None:
  """Node keep-alive endpoint.

  Updates ``last_seen_at`` and the self-reported ``max_threads`` for the
  calling node.  Returns 204 No Content on success.
  """
  node.last_seen_at = datetime.now(UTC)
  if body.max_threads is not None:
    node.max_threads = body.max_threads
  await session.commit()
