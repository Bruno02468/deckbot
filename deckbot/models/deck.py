from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from deckbot.models.sol import SolType


class DeckProperties(BaseModel):
  """Properties extracted from parsing the content of a deck file."""

  sol: SolType | None
  grid_count: int


class DeckInfo(BaseModel):
  """Display-oriented view of a stored deck."""

  id: int
  filename: str
  sol: SolType | None
  grid_count: int
  size_bytes: int
  source_channel_id: int | None
  source_url: str | None
  discovered_at: datetime
  tags: list[str]

  model_config = {"from_attributes": True}
