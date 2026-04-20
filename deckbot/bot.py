import asyncio
import logging
import signal

import discord
from discord.ext import commands
from sqlalchemy import select

from deckbot.config import get_settings
from deckbot.db.models import Channel, Job
from deckbot.db.session import enable_wal, get_session
from deckbot.models.job import CrawlChannelPayload
from deckbot.services.job_runner import JobRunner

log = logging.getLogger(__name__)

COGS: list[str] = [
  "deckbot.cogs.listener",
  "deckbot.cogs.admin",
  "deckbot.cogs.runs",
  "deckbot.cogs.decks",
]


class DeckBot(commands.Bot):
  def __init__(self) -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    super().__init__(
      command_prefix=commands.when_mentioned,
      intents=intents,
      help_command=None,
    )
    # Set of Discord channel IDs the bot is currently tracking.
    self.tracked_channel_ids: set[int] = set()
    self._job_runner: JobRunner | None = None

  async def setup_hook(self) -> None:
    settings = get_settings()

    await enable_wal()
    await self._load_tracked_channels()

    for cog in COGS:
      await self.load_extension(cog)
      log.info("Loaded cog: %s", cog)

    self._job_runner = JobRunner(self)
    self._job_runner.start()

    guild = discord.Object(id=settings.discord_guild_id)
    self.tree.copy_global_to(guild=guild)
    synced = await self.tree.sync(guild=guild)
    log.info(
      "Synced %d slash command(s) to guild %d",
      len(synced),
      settings.discord_guild_id,
    )

  async def _load_tracked_channels(self) -> None:
    async with get_session() as session:
      result = await session.execute(select(Channel))
      channels = result.scalars().all()
      self.tracked_channel_ids = {c.channel_id for c in channels}
      # Enqueue a catch-up crawl for every tracked channel so any messages
      # sent while the bot was down are picked up automatically.
      for ch in channels:
        session.add(
          Job(
            type="crawl_channel",
            status="pending",
            payload=CrawlChannelPayload(
              channel_id=ch.channel_id
            ).model_dump_json(),
          )
        )
      if channels:
        await session.commit()
        log.info("Queued catch-up crawl for %d channel(s)", len(channels))
    log.info("Tracking %d channel(s)", len(self.tracked_channel_ids))

  async def refresh_tracked_channels(self) -> None:
    """Reload the tracked-channel set from the database.

    Call this after adding or removing a channel via admin commands.
    """
    await self._load_tracked_channels()

  async def on_ready(self) -> None:
    assert self.user is not None
    log.info("Logged in as %s (ID: %d)", self.user, self.user.id)

  async def close(self) -> None:
    log.info("Shutting down...")
    if self._job_runner is not None:
      self._job_runner.stop()
    await super().close()


async def run_bot() -> None:
  logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
  )
  settings = get_settings()
  bot = DeckBot()

  loop = asyncio.get_event_loop()
  loop.add_signal_handler(
    signal.SIGTERM,
    lambda: asyncio.ensure_future(bot.close()),
  )

  async with bot:
    await bot.start(settings.discord_token)
