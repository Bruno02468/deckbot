from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.api.deps import get_db_session
from deckbot.db.models import Node
from deckbot.db.queries import get_node_by_key_hash

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def require_active_node(
  raw_key: str = Security(_api_key_header),
  session: AsyncSession = Depends(get_db_session),
) -> Node:
  """Dependency: authenticate a node by its API key.

  Hashes the provided key and looks it up in the nodes table.
  Raises 401 if the key is invalid or the node is inactive.
  """
  key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
  node = await get_node_by_key_hash(session, key_hash)
  if node is None:
    raise HTTPException(status_code=401, detail="Invalid or inactive API key")
  return node
