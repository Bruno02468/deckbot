from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.db.models import Channel, Deck, DeckTag, Job, Setting
from deckbot.models.deck import DeckInfo
from deckbot.models.sol import SolType

DECKS_PER_PAGE = 5


# ── Settings ─────────────────────────────────────────────────────────────────


async def get_setting(session: AsyncSession, key: str) -> str | None:
  row = await session.get(Setting, key)
  return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
  row = await session.get(Setting, key)
  if row is None:
    session.add(Setting(key=key, value=value))
  else:
    row.value = value


async def get_deckbot_channel_id(session: AsyncSession) -> int | None:
  v = await get_setting(session, "deckbot_channel_id")
  if v is None:
    return None
  try:
    return int(v)
  except ValueError:
    return None


# ── Decks ─────────────────────────────────────────────────────────────────────


async def count_decks(session: AsyncSession) -> int:
  result = await session.execute(select(func.count()).select_from(Deck))
  return result.scalar_one()


async def list_decks(
  session: AsyncSession, page: int
) -> tuple[list[DeckInfo], int]:
  """Return (page_of_decks, total_count) ordered newest-first."""
  total = await count_decks(session)
  offset = (page - 1) * DECKS_PER_PAGE
  result = await session.execute(
    select(Deck)
    .options(selectinload(Deck.tags))
    .order_by(Deck.discovered_at.desc())
    .offset(offset)
    .limit(DECKS_PER_PAGE)
  )
  infos = [
    DeckInfo(
      id=d.id,
      filename=d.filename,
      sol=d.sol,
      grid_count=d.grid_count,
      size_bytes=d.size_bytes,
      source_channel_id=d.source_channel_id,
      source_url=d.source_url,
      discovered_at=d.discovered_at,
      tags=[t.tag for t in d.tags],
    )
    for d in result.scalars().all()
  ]
  return infos, total


# ── Channels ──────────────────────────────────────────────────────────────────


async def count_channels(session: AsyncSession) -> int:
  result = await session.execute(select(func.count()).select_from(Channel))
  return result.scalar_one()


async def list_channels(session: AsyncSession) -> list[Channel]:
  result = await session.execute(select(Channel).order_by(Channel.added_at))
  return result.scalars().all()


# ── Jobs ──────────────────────────────────────────────────────────────────────


async def count_jobs_by_status(session: AsyncSession) -> dict[str, int]:
  result = await session.execute(
    select(Job.status, func.count()).group_by(Job.status)
  )
  return {row[0]: row[1] for row in result}


async def list_recent_jobs(
  session: AsyncSession, limit: int = 10
) -> list[Job]:
  result = await session.execute(
    select(Job).order_by(Job.created_at.desc()).limit(limit)
  )
  return result.scalars().all()


# ── Deck lookup & search ──────────────────────────────────────────────────────


async def get_deck(session: AsyncSession, deck_id: int) -> Deck | None:
  result = await session.execute(
    select(Deck).options(selectinload(Deck.tags)).where(Deck.id == deck_id)
  )
  return result.scalar_one_or_none()


async def search_decks(
  session: AsyncSession,
  *,
  name: str | None = None,
  sol: SolType | None = None,
  min_grids: int | None = None,
  max_grids: int | None = None,
  tag: str | None = None,
  channel_id: int | None = None,
  page: int = 1,
) -> tuple[list[DeckInfo], int]:
  """Return (page_of_decks, total_count) matching all supplied filters."""
  query = select(Deck).options(selectinload(Deck.tags))

  if name is not None:
    query = query.where(Deck.filename.ilike(f"%{name}%"))
  if sol is not None:
    query = query.where(Deck.sol == sol.value)
  if min_grids is not None:
    query = query.where(Deck.grid_count >= min_grids)
  if max_grids is not None:
    query = query.where(Deck.grid_count <= max_grids)
  if tag is not None:
    query = query.where(
      Deck.id.in_(select(DeckTag.deck_id).where(DeckTag.tag == tag))
    )
  if channel_id is not None:
    query = query.where(Deck.source_channel_id == channel_id)

  count_q = select(func.count()).select_from(query.subquery())
  total: int = (await session.execute(count_q)).scalar_one()

  offset = (page - 1) * DECKS_PER_PAGE
  result = await session.execute(
    query.order_by(Deck.discovered_at.desc())
    .offset(offset)
    .limit(DECKS_PER_PAGE)
  )
  infos = [
    DeckInfo(
      id=d.id,
      filename=d.filename,
      sol=d.sol,
      grid_count=d.grid_count,
      size_bytes=d.size_bytes,
      source_channel_id=d.source_channel_id,
      source_url=d.source_url,
      discovered_at=d.discovered_at,
      tags=[t.tag for t in d.tags],
    )
    for d in result.scalars().all()
  ]
  return infos, total


async def fetch_deck_blobs(
  session: AsyncSession,
  *,
  name: str | None = None,
  sol: SolType | None = None,
  min_grids: int | None = None,
  max_grids: int | None = None,
  tag: str | None = None,
  channel_id: int | None = None,
) -> list[tuple[int, str, bytes]]:
  """Return (id, filename, compressed_content) for all matching decks."""
  query = select(Deck.id, Deck.filename, Deck.content)

  if name is not None:
    query = query.where(Deck.filename.ilike(f"%{name}%"))
  if sol is not None:
    query = query.where(Deck.sol == sol.value)
  if min_grids is not None:
    query = query.where(Deck.grid_count >= min_grids)
  if max_grids is not None:
    query = query.where(Deck.grid_count <= max_grids)
  if tag is not None:
    query = query.where(
      Deck.id.in_(select(DeckTag.deck_id).where(DeckTag.tag == tag))
    )
  if channel_id is not None:
    query = query.where(Deck.source_channel_id == channel_id)

  result = await session.execute(query.order_by(Deck.discovered_at.desc()))
  return [(row[0], row[1], row[2]) for row in result]


# ── Tagging ───────────────────────────────────────────────────────────────────


async def add_tag(
  session: AsyncSession,
  deck_id: int,
  tag: str,
  tagged_by: int,
) -> bool:
  """Add a tag to a deck. Returns False if the tag already exists."""
  existing = await session.get(DeckTag, (deck_id, tag))
  if existing is not None:
    return False
  session.add(
    DeckTag(
      deck_id=deck_id,
      tag=tag,
      tagged_by=tagged_by,
      tagged_at=datetime.now(UTC),
    )
  )
  return True


async def remove_tag(
  session: AsyncSession,
  deck_id: int,
  tag: str,
) -> bool:
  """Remove a tag from a deck. Returns False if the tag did not exist."""
  existing = await session.get(DeckTag, (deck_id, tag))
  if existing is None:
    return False
  await session.delete(existing)
  return True
