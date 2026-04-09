from __future__ import annotations

import logging
from pathlib import PurePosixPath

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.db.models import Deck, ProcessedMessage
from deckbot.services.deck_parser import compress_deck, hash_deck, parse_deck
from deckbot.services.zip_handler import DECK_EXTENSIONS, extract_decks

log = logging.getLogger(__name__)

_ZIP_EXT = ".zip"


def _extension(filename: str) -> str:
  suffix = PurePosixPath(filename).suffix.lower()
  return suffix


async def process_message(
  message: discord.Message,
  session: AsyncSession,
) -> int:
  """Inspect a Discord message and store any new decks found.

  The session is NOT committed here — the caller is responsible for
  committing (this allows batching in the crawler).

  Returns the number of new decks stored.
  """
  # Skip if we've already handled this message (listener/crawler overlap).
  already = await session.get(ProcessedMessage, message.id)
  if already is not None:
    return 0

  new_decks = 0
  for attachment in message.attachments:
    new_decks += await _process_attachment(attachment, message, session)

  # Mark processed regardless of whether any relevant attachments were found.
  session.add(
    ProcessedMessage(
      message_id=message.id,
      channel_id=message.channel.id,
    )
  )
  return new_decks


async def _process_attachment(
  attachment: discord.Attachment,
  message: discord.Message,
  session: AsyncSession,
) -> int:
  """Download and process a single attachment. Returns new deck count."""
  ext = _extension(attachment.filename)

  if ext == _ZIP_EXT:
    data = await attachment.read()
    pairs = extract_decks(data)
    count = 0
    for inner_filename, raw in pairs:
      if await _store_deck(
        raw=raw,
        filename=inner_filename,
        message=message,
        session=session,
      ):
        count += 1
    return count

  if ext in DECK_EXTENSIONS:
    raw = await attachment.read()
    stored = await _store_deck(
      raw=raw,
      filename=attachment.filename,
      message=message,
      session=session,
    )
    return 1 if stored else 0

  return 0


async def _store_deck(
  raw: bytes,
  filename: str,
  message: discord.Message,
  session: AsyncSession,
) -> bool:
  """Add a deck to the session if it is not already known.

  Returns True when the deck is new; False when it is a duplicate.
  Does NOT commit — the caller owns the transaction.
  """
  deck_hash = hash_deck(raw)

  # Check the database for an existing deck with the same hash.
  existing = await session.scalar(select(Deck).where(Deck.hash == deck_hash))
  if existing is not None:
    log.debug("Skipping duplicate deck %s (hash %.8s)", filename, deck_hash)
    return False

  # Also check objects that are already pending in this session (uncommitted
  # batch), to avoid a unique-constraint violation on commit.
  for pending in session.new:
    if isinstance(pending, Deck) and pending.hash == deck_hash:
      log.debug(
        "Skipping in-batch duplicate deck %s (hash %.8s)",
        filename,
        deck_hash,
      )
      return False

  props = parse_deck(raw)
  session.add(
    Deck(
      hash=deck_hash,
      filename=filename,
      sol=props.sol,
      grid_count=props.grid_count,
      size_bytes=len(raw),
      content=compress_deck(raw),
      source_message_id=message.id,
      source_channel_id=message.channel.id,
      source_url=message.jump_url,
    )
  )
  log.info(
    "Stored deck %s | SOL=%s | GRIDs=%d",
    filename,
    props.sol,
    props.grid_count,
  )
  return True
