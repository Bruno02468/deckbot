from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from deckbot.db.models import Channel, Job
from deckbot.db.queries import (
  count_channels,
  count_decks,
  count_jobs_by_status,
  get_deckbot_channel_id,
  list_channels,
  list_recent_jobs,
  set_setting,
)
from deckbot.db.session import get_session
from deckbot.models.job import CrawlChannelPayload, ReprocessChannelPayload
from deckbot.cogs._checks import _is_deckbot_admin, admin_check as _admin_check

if TYPE_CHECKING:
  from deckbot.bot import DeckBot

log = logging.getLogger(__name__)

# ── Permission helpers ────────────────────────────────────────────────────────

# _is_deckbot_admin and _admin_check are imported from deckbot.cogs._checks.

# ── Ephemeral helper ─────────────────────────────────────────────────────────


def _is_ephemeral(
  interaction: discord.Interaction,
  deckbot_channel_id: int | None,
) -> bool:
  return (
    deckbot_channel_id is None or interaction.channel_id != deckbot_channel_id
  )


# ── Job status formatting ─────────────────────────────────────────────────────

_STATUS_EMOJI = {
  "pending": "⏳",
  "running": "🔄",
  "completed": "✅",
  "failed": "❌",
}


# ── Cog ───────────────────────────────────────────────────────────────────────


class AdminCog(commands.Cog, name="Admin"):
  deckbot = app_commands.Group(
    name="deckbot",
    description="Bot administration",
    guild_only=True,
  )

  def __init__(self, bot: DeckBot) -> None:
    self.bot = bot

  async def cog_app_command_error(
    self,
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
  ) -> None:
    if isinstance(error, app_commands.CheckFailure):
      if not interaction.response.is_done():
        await interaction.response.send_message(str(error), ephemeral=True)
    else:
      log.exception(
        "Unhandled error in /%s",
        interaction.command and interaction.command.qualified_name,
      )

  # ── /deckbot setup ────────────────────────────────────────────────────────

  @deckbot.command(
    name="setup",
    description="Set the designated deckbot channel",
  )
  @app_commands.describe(
    channel="Channel where the bot will post and accept commands"
  )
  @app_commands.check(_admin_check)
  async def setup(
    self,
    interaction: discord.Interaction,
    channel: discord.TextChannel,
  ) -> None:
    async with get_session() as session:
      await set_setting(session, "deckbot_channel_id", str(channel.id))
      await session.commit()
      deckbot_ch_id = channel.id

    ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
    await interaction.response.send_message(
      f"Deckbot channel set to {channel.mention}.",
      ephemeral=ephemeral,
    )

  # ── /deckbot track ────────────────────────────────────────────────────────

  @deckbot.command(
    name="track",
    description="Start tracking a channel for deck attachments",
  )
  @app_commands.describe(channel="Channel to track")
  @app_commands.check(_admin_check)
  async def track(
    self,
    interaction: discord.Interaction,
    channel: discord.TextChannel,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      existing = await session.get(Channel, channel.id)
      if existing is not None:
        await interaction.response.send_message(
          f"{channel.mention} is already being tracked.",
          ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
        )
        return

      session.add(
        Channel(
          channel_id=channel.id,
          guild_id=channel.guild.id,
          name=channel.name,
        )
      )
      # Enqueue an initial crawl for the newly added channel.
      session.add(
        Job(
          type="crawl_channel",
          status="pending",
          payload=CrawlChannelPayload(channel_id=channel.id).model_dump_json(),
        )
      )
      await session.commit()

    await self.bot.refresh_tracked_channels()

    ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
    await interaction.response.send_message(
      f"Now tracking {channel.mention}. An initial crawl has been queued.",
      ephemeral=ephemeral,
    )

  # ── /deckbot untrack ──────────────────────────────────────────────────────

  @deckbot.command(
    name="untrack",
    description="Stop tracking a channel",
  )
  @app_commands.describe(channel="Channel to stop tracking")
  @app_commands.check(_admin_check)
  async def untrack(
    self,
    interaction: discord.Interaction,
    channel: discord.TextChannel,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      existing = await session.get(Channel, channel.id)
      if existing is None:
        await interaction.response.send_message(
          f"{channel.mention} is not currently tracked.",
          ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
        )
        return

      await session.delete(existing)
      await session.commit()

    await self.bot.refresh_tracked_channels()

    ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
    await interaction.response.send_message(
      f"Stopped tracking {channel.mention}.",
      ephemeral=ephemeral,
    )

  # ── /deckbot channels ─────────────────────────────────────────────────────

  @deckbot.command(
    name="channels",
    description="List currently tracked channels",
  )
  @app_commands.check(_admin_check)
  async def channels(self, interaction: discord.Interaction) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      rows = await list_channels(session)

    if not rows:
      body = "_No channels are currently tracked._"
    else:
      lines = [
        f"<#{r.channel_id}> (`#{r.name}`, ID: `{r.channel_id}`)" for r in rows
      ]
      body = "\n".join(lines)

    embed = discord.Embed(
      title=f"Tracked Channels ({len(rows)})",
      description=body,
      colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(
      embed=embed,
      ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
    )

  # ── /deckbot crawl ────────────────────────────────────────────────────────

  @deckbot.command(
    name="crawl",
    description="Schedule a full crawl of a tracked channel",
  )
  @app_commands.describe(channel="Tracked channel to crawl")
  @app_commands.check(_admin_check)
  async def crawl(
    self,
    interaction: discord.Interaction,
    channel: discord.TextChannel,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      existing = await session.get(Channel, channel.id)
      if existing is None:
        await interaction.response.send_message(
          f"{channel.mention} is not tracked. "
          "Use `/deckbot track` to add it first.",
          ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
        )
        return

      session.add(
        Job(
          type="crawl_channel",
          status="pending",
          payload=CrawlChannelPayload(channel_id=channel.id).model_dump_json(),
        )
      )
      await session.commit()

    await interaction.response.send_message(
      f"Crawl job queued for {channel.mention}.",
      ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
    )

  # ── /deckbot reprocess ───────────────────────────────────────────────────

  @deckbot.command(
    name="reprocess",
    description="Re-parse stored deck BLOBs to update sol and grid count",
  )
  @app_commands.describe(
    channel="Channel to reprocess (omit to reprocess all tracked channels)"
  )
  @app_commands.check(_admin_check)
  async def reprocess(
    self,
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if channel is not None:
        existing = await session.get(Channel, channel.id)
        if existing is None:
          await interaction.response.send_message(
            f"{channel.mention} is not tracked. "
            "Use `/deckbot track` to add it first.",
            ephemeral=ephemeral,
          )
          return
        channels_to_reprocess = [existing]
      else:
        channels_to_reprocess = await list_channels(session)
        if not channels_to_reprocess:
          await interaction.response.send_message(
            "No channels are currently tracked.",
            ephemeral=ephemeral,
          )
          return

      for ch in channels_to_reprocess:
        session.add(
          Job(
            type="reprocess_channel",
            status="pending",
            payload=ReprocessChannelPayload(
              channel_id=ch.channel_id
            ).model_dump_json(),
          )
        )
      await session.commit()

    if channel is not None:
      msg = f"Reprocess job queued for {channel.mention}."
    else:
      msg = (
        f"Reprocess jobs queued for all "
        f"{len(channels_to_reprocess)} tracked channel(s)."
      )
    await interaction.response.send_message(msg, ephemeral=ephemeral)

  # ── /deckbot status ───────────────────────────────────────────────────────

  @deckbot.command(
    name="status",
    description="Show bot status: decks, channels, and job counts",
  )
  @app_commands.check(_admin_check)
  async def status(self, interaction: discord.Interaction) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      n_decks = await count_decks(session)
      channel_rows = await list_channels(session)
      job_counts = await count_jobs_by_status(session)

    ch_list = (
      "\n".join(f"  • <#{r.channel_id}> (`#{r.name}`)" for r in channel_rows)
      if channel_rows
      else "  _none_"
    )

    pending = job_counts.get("pending", 0)
    running = job_counts.get("running", 0)
    completed = job_counts.get("completed", 0)
    failed = job_counts.get("failed", 0)

    home = f"<#{deckbot_ch_id}>" if deckbot_ch_id else "_not configured_"

    embed = discord.Embed(
      title="DeckBot Status",
      colour=discord.Colour.green(),
    )
    embed.add_field(name="Home channel", value=home, inline=False)
    embed.add_field(name="Decks stored", value=str(n_decks), inline=True)
    embed.add_field(
      name="Channels tracked",
      value=str(len(channel_rows)),
      inline=True,
    )
    embed.add_field(
      name="Tracked channels",
      value=ch_list,
      inline=False,
    )
    embed.add_field(
      name="Jobs",
      value=(
        f"⏳ pending: **{pending}**  "
        f"🔄 running: **{running}**  "
        f"✅ completed: **{completed}**  "
        f"❌ failed: **{failed}**"
      ),
      inline=False,
    )

    await interaction.response.send_message(
      embed=embed,
      ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
    )

  # ── /deckbot jobs ─────────────────────────────────────────────────────────

  @deckbot.command(
    name="jobs",
    description="List the 10 most recent jobs",
  )
  @app_commands.check(_admin_check)
  async def jobs(self, interaction: discord.Interaction) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      rows = await list_recent_jobs(session, limit=10)

    if not rows:
      description = "_No jobs recorded yet._"
    else:
      lines: list[str] = []
      for job in rows:
        emoji = _STATUS_EMOJI.get(job.status, "❓")
        ts = job.created_at.strftime("%Y-%m-%d %H:%M UTC")
        error_suffix = ""
        if job.error:
          # Truncate long error messages to keep the embed readable.
          short = job.error[:60].replace("\n", " ")
          if len(job.error) > 60:
            short += "…"
          error_suffix = f" — `{short}`"
        lines.append(
          f"`#{job.id}` {emoji} **{job.type}** · {ts}{error_suffix}"
        )
      description = "\n".join(lines)

    embed = discord.Embed(
      title="Recent Jobs",
      description=description,
      colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(
      embed=embed,
      ephemeral=_is_ephemeral(interaction, deckbot_ch_id),
    )


async def setup(bot: commands.Bot) -> None:
  await bot.add_cog(AdminCog(bot))  # type: ignore[arg-type]
