from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from deckbot.db.session import get_session
from deckbot.services.processor import process_message

if TYPE_CHECKING:
  from deckbot.bot import DeckBot

log = logging.getLogger(__name__)


class ListenerCog(commands.Cog, name="Listener"):
  """Listens for new messages in tracked channels and stores any decks."""

  def __init__(self, bot: DeckBot) -> None:
    self.bot = bot

  @commands.Cog.listener()
  async def on_message(self, message: discord.Message) -> None:
    # Ignore DMs and bot messages.
    if not message.guild or message.author.bot:
      return

    # Accept messages from tracked channels, or threads inside them.
    ch = message.channel
    if isinstance(ch, discord.Thread):
      if ch.parent_id not in self.bot.tracked_channel_ids:
        return
    elif ch.id not in self.bot.tracked_channel_ids:
      return

    # Skip messages with no attachments early.
    if not message.attachments:
      return

    try:
      async with get_session() as session:
        new_decks = await process_message(message, session)
        await session.commit()
    except Exception:
      log.exception(
        "Error processing message %d in channel %d",
        message.id,
        message.channel.id,
      )
      return

    if new_decks:
      log.info(
        "Stored %d new deck(s) from message %d in channel %d",
        new_decks,
        message.id,
        message.channel.id,
      )


async def setup(bot: commands.Bot) -> None:
  await bot.add_cog(ListenerCog(bot))  # type: ignore[arg-type]
