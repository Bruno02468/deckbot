from __future__ import annotations

import logging

import zstandard as zstd
from sqlalchemy import select

from deckbot.db.models import Deck
from deckbot.db.session import AsyncSession
from deckbot.services.deck_parser import parse_deck

log = logging.getLogger(__name__)

_DECOMPRESSOR = zstd.ZstdDecompressor()
_BATCH_SIZE = 50


async def reprocess_channel(channel_id: int, session: AsyncSession) -> int:
  """Re-parse stored deck BLOBs for a channel, updating sol and grid_count.

  Returns the number of decks updated.
  """
  result = await session.execute(
    select(Deck).where(Deck.source_channel_id == channel_id)
  )
  decks = result.scalars().all()

  updated = 0
  for i, deck in enumerate(decks):
    raw = _DECOMPRESSOR.decompress(deck.content)
    props = parse_deck(raw)
    deck.sol = props.sol
    deck.grid_count = props.grid_count
    updated += 1

    if (i + 1) % _BATCH_SIZE == 0:
      await session.commit()
      log.debug(
        "Reprocess channel %d: committed batch at %d", channel_id, i + 1
      )

  await session.commit()
  log.info("Reprocess channel %d: updated %d deck(s)", channel_id, updated)
  return updated
