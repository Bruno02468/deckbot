from __future__ import annotations

import io
import logging
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import discord
import zstandard as zstd
from discord import app_commands
from discord.ext import commands

from deckbot.cogs._checks import _is_deckbot_admin, admin_check as _admin_check
from deckbot.db.queries import (
  DECKS_PER_PAGE,
  add_tag,
  fetch_deck_blobs,
  get_deck,
  get_deckbot_channel_id,
  remove_tag,
  search_decks,
)
from deckbot.db.session import get_session
from deckbot.models.deck import DeckInfo
from deckbot.models.sol import SolType, normalize_sol

if TYPE_CHECKING:
  from deckbot.bot import DeckBot

log = logging.getLogger(__name__)

# Predefined tag values.
_TAGS: list[str] = [
  "should_fatal",
  "incompatible",
  "bad_result",
  "slow",
  "big",
]

# Buttons become non-interactive after this many seconds of no use.
_DECOMPRESSOR = zstd.ZstdDecompressor()


def _build_zip(blobs: list[tuple[int, str, bytes]]) -> io.BytesIO:
  """Build a zip archive from (id, filename, compressed_content) tuples.

  Filenames that collide across different deck IDs are de-duplicated by
  inserting the deck ID before the extension: deck.dat → deck-42.dat.
  """
  # Count how many times each filename appears.
  name_counts: dict[str, int] = {}
  for _, filename, _ in blobs:
    name_counts[filename] = name_counts.get(filename, 0) + 1

  buf = io.BytesIO()
  with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
    for deck_id, filename, compressed in blobs:
      raw = _DECOMPRESSOR.decompress(compressed)
      if name_counts[filename] > 1:
        p = PurePosixPath(filename)
        arc_name = f"{p.stem}-{deck_id}{p.suffix}"
      else:
        arc_name = filename
      zf.writestr(arc_name, raw)
  buf.seek(0)
  return buf


_VIEW_TIMEOUT = 120.0


def _fmt_size(n: int) -> str:
  if n < 1024:
    return f"{n} B"
  if n < 1024 * 1024:
    return f"{n / 1024:.1f} KB"
  return f"{n / 1024 / 1024:.1f} MB"


def _fmt_deck(deck: DeckInfo) -> tuple[str, str]:
  """Return (field_name, field_value) for a deck embed field."""
  name = f"#{deck.id} {deck.filename}"
  parts = [
    f"SOL: `{deck.sol or '—'}`",
    f"GRIDs: `{deck.grid_count}`",
    f"Size: `{_fmt_size(deck.size_bytes)}`",
  ]
  if deck.tags:
    parts.append("Tags: " + ", ".join(f"`{t}`" for t in deck.tags))
  value = " · ".join(parts)
  # Source line: channel mention, date, and jump link together.
  date_str = deck.discovered_at.strftime("%Y-%m-%d")
  if deck.source_url and deck.source_channel_id:
    value += (
      f"\n<#{deck.source_channel_id}> · {date_str} · [jump]({deck.source_url})"
    )
  elif deck.source_url:
    value += f"\n{date_str} · [jump]({deck.source_url})"
  else:
    value += f"\n{date_str}"
  return name, value


def _is_ephemeral(
  interaction: discord.Interaction, deckbot_channel_id: int | None
) -> bool:
  return (
    deckbot_channel_id is None or interaction.channel_id != deckbot_channel_id
  )


def _build_embed(
  decks: list[DeckInfo],
  page: int,
  total_pages: int,
  total: int,
  title_prefix: str,
) -> discord.Embed:
  embed = discord.Embed(
    title=f"{title_prefix} — Page {page}/{total_pages} (total: {total})",
    colour=discord.Colour.orange(),
  )
  for deck_info in decks:
    field_name, field_value = _fmt_deck(deck_info)
    embed.add_field(name=field_name, value=field_value, inline=False)
  return embed


@dataclass
class _SearchParams:
  """Captured filter state shared between the command and the paginator."""

  name: str | None = None
  sol: SolType | None = None
  min_grids: int | None = None
  max_grids: int | None = None
  tag: str | None = None
  channel_id: int | None = None


class DeckPageView(discord.ui.View):
  """Prev / Next buttons for paged deck results."""

  def __init__(
    self,
    params: _SearchParams,
    page: int,
    total_pages: int,
    total: int,
    ephemeral: bool,
    invoker_id: int,
  ) -> None:
    super().__init__(timeout=_VIEW_TIMEOUT)
    self._params = params
    self._page = page
    self._total_pages = total_pages
    self._total = total
    self._ephemeral = ephemeral
    self._invoker_id = invoker_id
    self._update_buttons()

  def _update_buttons(self) -> None:
    self.prev_button.disabled = self._page <= 1
    self.next_button.disabled = self._page >= self._total_pages

  async def _go_to_page(
    self, interaction: discord.Interaction, new_page: int
  ) -> None:
    if interaction.user.id != self._invoker_id:
      await interaction.response.send_message(
        "These buttons belong to someone else's search.", ephemeral=True
      )
      return
    p = self._params
    async with get_session() as session:
      decks, total = await search_decks(
        session,
        name=p.name,
        sol=p.sol,
        min_grids=p.min_grids,
        max_grids=p.max_grids,
        tag=p.tag,
        channel_id=p.channel_id,
        page=new_page,
      )

    total_pages = max(1, math.ceil(total / DECKS_PER_PAGE))
    self._page = new_page
    self._total = total
    self._total_pages = total_pages
    self._update_buttons()

    title_prefix = (
      "Search Results"
      if any(
        [
          p.name,
          p.sol,
          p.min_grids,
          p.max_grids,
          p.tag,
          p.channel_id,
        ]
      )
      else "Decks"
    )
    embed = _build_embed(decks, new_page, total_pages, total, title_prefix)
    await interaction.response.edit_message(embed=embed, view=self)

  @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
  async def prev_button(
    self, interaction: discord.Interaction, button: discord.ui.Button[Any]
  ) -> None:
    await self._go_to_page(interaction, self._page - 1)

  @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
  async def next_button(
    self, interaction: discord.Interaction, button: discord.ui.Button[Any]
  ) -> None:
    await self._go_to_page(interaction, self._page + 1)

  @discord.ui.button(label="⬇ Download All", style=discord.ButtonStyle.primary)
  async def download_button(
    self, interaction: discord.Interaction, button: discord.ui.Button[Any]
  ) -> None:
    if interaction.user.id != self._invoker_id:
      await interaction.response.send_message(
        "These buttons belong to someone else's search.", ephemeral=True
      )
      return
    await interaction.response.defer(ephemeral=True, thinking=True)
    p = self._params
    async with get_session() as session:
      blobs = await fetch_deck_blobs(
        session,
        name=p.name,
        sol=p.sol,
        min_grids=p.min_grids,
        max_grids=p.max_grids,
        tag=p.tag,
        channel_id=p.channel_id,
      )
    if not blobs:
      await interaction.followup.send("No decks to download.", ephemeral=True)
      return
    zip_buf = _build_zip(blobs)
    await interaction.followup.send(
      f"{len(blobs)} deck(s) zipped.",
      file=discord.File(zip_buf, filename="decks.zip"),
      ephemeral=self._ephemeral,
    )

  async def on_timeout(self) -> None:
    # Disable buttons in-place when the view expires.
    self.prev_button.disabled = True
    self.next_button.disabled = True
    self.download_button.disabled = True
    if self.message is not None:
      try:
        await self.message.edit(view=self)
      except discord.HTTPException:
        pass


class DecksCog(commands.Cog, name="Decks"):
  deck = app_commands.Group(
    name="deck",
    description="Query stored decks",
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
      await interaction.response.send_message(str(error), ephemeral=True)
    else:
      log.exception("Unhandled error in DecksCog command", exc_info=error)

  # ── /deck list ────────────────────────────────────────────────────────────

  @deck.command(
    name="list",
    description="List all stored decks, newest first",
  )
  @app_commands.describe(page="Page number (default: 1)")
  async def list_decks_cmd(
    self,
    interaction: discord.Interaction,
    page: int = 1,
  ) -> None:
    await self._run_search(interaction, page=page)

  # ── /deck search ──────────────────────────────────────────────────────────

  @deck.command(
    name="search",
    description="Search stored decks with optional filters",
  )
  @app_commands.describe(
    name="Filename substring (case-insensitive)",
    sol='Solution type, e.g. "statics" or "other" for unrecognized SOLs',
    min_grids="Minimum GRID count (inclusive)",
    max_grids="Maximum GRID count (inclusive)",
    tag="Filter to decks that have this tag",
    channel="Filter to decks found in this channel",
    page="Page number (default: 1)",
  )
  async def search_decks_cmd(
    self,
    interaction: discord.Interaction,
    name: str | None = None,
    sol: str | None = None,
    min_grids: int | None = None,
    max_grids: int | None = None,
    tag: str | None = None,
    channel: discord.TextChannel | None = None,
    page: int = 1,
  ) -> None:
    await self._run_search(
      interaction,
      name=name,
      sol=sol,
      min_grids=min_grids,
      max_grids=max_grids,
      tag=tag,
      channel=channel,
      page=page,
    )

  async def _run_search(
    self,
    interaction: discord.Interaction,
    *,
    name: str | None = None,
    sol: str | None = None,
    min_grids: int | None = None,
    max_grids: int | None = None,
    tag: str | None = None,
    channel: discord.TextChannel | None = None,
    page: int = 1,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if page < 1:
        await interaction.response.send_message(
          "Page must be 1 or greater.", ephemeral=True
        )
        return

      # "other" is the user-facing alias for the "unknown" canonical value.
      # Raw aliases (e.g. "103") are also accepted via normalize_sol().
      sol_filter: SolType | None = None
      if sol is not None:
        if sol == "other":
          sol_filter = SolType.unknown
        else:
          sol_filter = normalize_sol(sol)
          if sol_filter is None or sol_filter == SolType.unknown:
            await interaction.response.send_message(
              f"`{sol}` is not a recognised SOL type. "
              'Use "other" to search for unrecognised SOLs.',
              ephemeral=True,
            )
            return

      channel_id = channel.id if channel is not None else None
      decks, total = await search_decks(
        session,
        name=name,
        sol=sol_filter,
        min_grids=min_grids,
        max_grids=max_grids,
        tag=tag,
        channel_id=channel_id,
        page=page,
      )

    total_pages = max(1, math.ceil(total / DECKS_PER_PAGE))

    if page > total_pages:
      await interaction.response.send_message(
        f"Page {page} does not exist. There are only {total_pages} page(s).",
        ephemeral=True,
      )
      return

    has_filters = any(
      [name, sol_filter, min_grids, max_grids, tag, channel_id]
    )
    if not decks:
      msg = (
        "No decks match that search."
        if has_filters
        else "No decks stored yet."
      )
      await interaction.response.send_message(msg, ephemeral=ephemeral)
      return

    params = _SearchParams(
      name=name,
      sol=sol_filter,
      min_grids=min_grids,
      max_grids=max_grids,
      tag=tag,
      channel_id=channel_id,
    )
    title_prefix = "Search Results" if has_filters else "Decks"
    view = DeckPageView(
      params,
      page,
      total_pages,
      total,
      ephemeral,
      invoker_id=interaction.user.id,
    )
    embed = _build_embed(decks, page, total_pages, total, title_prefix)
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=ephemeral
    )
    view.message = await interaction.original_response()

  @search_decks_cmd.autocomplete("sol")
  async def _sol_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    choices = [
      app_commands.Choice(name=v, value=v)
      for v in [*[s.value for s in SolType if s != SolType.unknown], "other"]
      if current.lower() in v.lower()
    ]
    return choices[:25]

  @search_decks_cmd.autocomplete("tag")
  async def _search_tag_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=t, value=t)
      for t in _TAGS
      if current.lower() in t.lower()
    ]

  # ── /deck tag ─────────────────────────────────────────────────────────────

  @deck.command(
    name="tag",
    description="Add a tag to a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID (the # number shown in /deck list)",
    tag="Tag to apply",
  )
  @app_commands.check(_admin_check)
  async def tag_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int,
    tag: str,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if tag not in _TAGS:
        valid = ", ".join(f"`{t}`" for t in _TAGS)
        await interaction.response.send_message(
          f"`{tag}` is not a valid tag. Valid tags: {valid}",
          ephemeral=True,
        )
        return

      deck = await get_deck(session, deck_id)
      if deck is None:
        await interaction.response.send_message(
          f"No deck with ID `{deck_id}`.", ephemeral=True
        )
        return

      added = await add_tag(session, deck_id, tag, interaction.user.id)
      if not added:
        await interaction.response.send_message(
          f"Deck `#{deck_id}` already has the `{tag}` tag.",
          ephemeral=ephemeral,
        )
        return

      await session.commit()

    await interaction.response.send_message(
      f"Tagged deck `#{deck_id}` with `{tag}`.", ephemeral=ephemeral
    )

  @tag_cmd.autocomplete("tag")
  async def _tag_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=t, value=t)
      for t in _TAGS
      if current.lower() in t.lower()
    ]

  # ── /deck untag ───────────────────────────────────────────────────────────

  @deck.command(
    name="untag",
    description="Remove a tag from a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID (the # number shown in /deck list)",
    tag="Tag to remove",
  )
  @app_commands.check(_admin_check)
  async def untag_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int,
    tag: str,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      deck = await get_deck(session, deck_id)
      if deck is None:
        await interaction.response.send_message(
          f"No deck with ID `{deck_id}`.", ephemeral=True
        )
        return

      removed = await remove_tag(session, deck_id, tag)
      if not removed:
        await interaction.response.send_message(
          f"Deck `#{deck_id}` does not have the `{tag}` tag.",
          ephemeral=ephemeral,
        )
        return

      await session.commit()

    await interaction.response.send_message(
      f"Removed tag `{tag}` from deck `#{deck_id}`.", ephemeral=ephemeral
    )

  @untag_cmd.autocomplete("tag")
  async def _untag_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=t, value=t)
      for t in _TAGS
      if current.lower() in t.lower()
    ]


async def setup(bot: commands.Bot) -> None:
  await bot.add_cog(DecksCog(bot))  # type: ignore[arg-type]
