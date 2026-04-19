from __future__ import annotations

import logging

import discord
from discord.abc import Messageable
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.db.models import Channel
from deckbot.services.processor import process_message

log = logging.getLogger(__name__)

# Number of messages to process before committing and updating the checkpoint.
_COMMIT_EVERY = 100


async def _crawl_thread(
  thread: discord.Thread,
  session: AsyncSession,
) -> int:
  """Crawl all messages in a single thread, returning the message count."""
  count = 0

  async for message in thread.history(limit=None, oldest_first=True):
    try:
      await process_message(message, session)
    except Exception:
      log.exception(
        "Error processing message %d in thread %d (%s)",
        message.id,
        thread.id,
        thread.name,
      )

    count += 1

    if count % _COMMIT_EVERY == 0:
      await session.commit()
      log.info(
        "Thread crawl checkpoint: %d messages in thread %d (%s)",
        count,
        thread.id,
        thread.name,
      )

  await session.commit()
  log.info(
    "Thread crawl complete: %d messages in thread %d (%s)",
    count,
    thread.id,
    thread.name,
  )
  return count


async def _crawl_all_threads(
  channel: discord.TextChannel,
  session: AsyncSession,
) -> int:
  """Crawl all active and public archived threads of *channel*."""
  threads: list[discord.Thread] = []

  # Active threads (guild-wide, filtered to this channel).
  try:
    active = await channel.guild.active_threads()
    threads.extend(t for t in active if t.parent_id == channel.id)
  except discord.Forbidden:
    log.warning(
      "No permission to fetch active threads in channel %d", channel.id
    )

  # Public archived threads.
  try:
    async for thread in channel.archived_threads(limit=None):
      threads.append(thread)
  except discord.Forbidden:
    log.warning(
      "No permission to fetch archived threads in channel %d", channel.id
    )

  log.info(
    "Found %d thread(s) to crawl in channel %d",
    len(threads),
    channel.id,
  )

  total = 0
  for thread in threads:
    total += await _crawl_thread(thread, session)

  return total


async def crawl_channel(
  channel_id: int,
  bot: commands.Bot,
  session: AsyncSession,
) -> None:
  """Crawl a channel's full history, storing any new decks found.

  Resumes from the last checkpoint stored in channels.last_crawled_message_id.
  Updates that checkpoint every _COMMIT_EVERY messages and on completion.
  """
  # Resolve the channel — prefer the cache to avoid an extra API call.
  raw_channel = bot.get_channel(channel_id)
  if raw_channel is None:
    raw_channel = await bot.fetch_channel(channel_id)

  if not isinstance(raw_channel, Messageable):
    raise TypeError(
      f"Channel {channel_id} is not a messageable channel "
      f"(got {type(raw_channel).__name__})"
    )

  channel = raw_channel

  # Load crawl checkpoint from the DB.
  channel_record = await session.get(Channel, channel_id)
  after_id = channel_record.last_crawled_message_id if channel_record else None
  after_obj = discord.Object(id=after_id) if after_id else None

  log.info(
    "Starting crawl of channel %d (after message %s)",
    channel_id,
    after_id,
  )

  count = 0
  last_id: int | None = None

  async for message in channel.history(
    limit=None, oldest_first=True, after=after_obj
  ):
    try:
      await process_message(message, session)
    except Exception:
      log.exception(
        "Error processing message %d in channel %d",
        message.id,
        channel_id,
      )

    last_id = message.id
    count += 1

    if count % _COMMIT_EVERY == 0:
      if channel_record is not None and last_id is not None:
        channel_record.last_crawled_message_id = last_id
      await session.commit()
      log.info(
        "Crawl checkpoint: %d messages processed in channel %d",
        count,
        channel_id,
      )

  # Final commit — save any remainder below the _COMMIT_EVERY threshold.
  if channel_record is not None and last_id is not None:
    channel_record.last_crawled_message_id = last_id
  await session.commit()

  log.info(
    "Crawl complete: %d messages processed in channel %d",
    count,
    channel_id,
  )

  # Also crawl threads belonging to this channel.
  if isinstance(channel, discord.TextChannel):
    thread_count = await _crawl_all_threads(channel, session)
    log.info(
      "Thread crawl complete: %d total messages across all threads "
      "in channel %d",
      thread_count,
      channel_id,
    )
