from __future__ import annotations

import io
import logging
import math
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import discord
import zstandard as zstd
from discord import app_commands
from discord.ext import commands

from deckbot.cogs._checks import _is_deckbot_admin, admin_check as _admin_check
from deckbot.db.models import Run
from deckbot.db.queries import (
  DECKS_PER_PAGE,
  add_tag,
  fetch_deck_blobs,
  get_active_run_for_deck_version,
  get_deck,
  get_deckbot_channel_id,
  get_or_create_version,
  get_run,
  list_runs_for_deck,
  remove_tag,
  search_decks,
)
from deckbot.db.session import get_session
from deckbot.models.deck import DeckInfo
from deckbot.models.repo import APPROVED_REPOS
from deckbot.models.sol import SolType, normalize_sol
from deckbot.services.version_resolver import ResolveError, resolve_ref

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

  # ── /deck run ─────────────────────────────────────────────────────────────

  @deck.command(
    name="run",
    description="Queue a MYSTRAN run for a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID to run",
    repo="Repository name (see /deck repos for the list)",
    ref="Branch, tag, or full commit SHA",
  )
  async def run_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int,
    repo: str,
    ref: str,
  ) -> None:
    await interaction.response.defer(thinking=True)

    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      deck = await get_deck(session, deck_id)
      if deck is None:
        await interaction.followup.send(
          f"No deck with ID `{deck_id}`.", ephemeral=True
        )
        return

      if repo not in APPROVED_REPOS:
        allowed = ", ".join(f"`{k}`" for k in APPROVED_REPOS)
        await interaction.followup.send(
          f"`{repo}` is not an approved repository. Allowed: {allowed}",
          ephemeral=True,
        )
        return

      try:
        commit_hash = await resolve_ref(repo, ref)
      except ResolveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

      version = await get_or_create_version(
        session,
        repo,
        commit_hash,
        ref_name=ref if ref != commit_hash else None,
      )
      await session.flush()

      active = await get_active_run_for_deck_version(
        session, deck_id, version.id
      )
      if active is not None:
        await interaction.followup.send(
          f"Deck `#{deck_id}` already has a `{active.status}` run "
          f"for `{repo}@{ref}` (run #{active.id}).",
          ephemeral=ephemeral,
        )
        return

      run = Run(
        deck_id=deck_id,
        version_id=version.id,
        status="pending",
        submitted_by=interaction.user.id,
        created_at=datetime.now(UTC),
      )
      session.add(run)
      await session.commit()
      run_id = run.id

    ref_display = ref if ref == commit_hash else f"{ref} ({commit_hash[:8]})"
    await interaction.followup.send(
      f"Run `#{run_id}` queued: deck `#{deck_id}` on `{repo}@{ref_display}`.",
      ephemeral=ephemeral,
    )

  @run_cmd.autocomplete("repo")
  async def _run_repo_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=k, value=k)
      for k in APPROVED_REPOS
      if current.lower() in k.lower()
    ][:25]

  # ── /deck run-bulk ────────────────────────────────────────────────────────

  @deck.command(
    name="run-bulk",
    description="Queue MYSTRAN runs for all decks matching optional filters",
  )
  @app_commands.describe(
    repo="Repository name",
    ref="Branch, tag, or full commit SHA",
    name="Filename substring filter",
    sol='SOL type filter (use "other" for unrecognised)',
    min_grids="Minimum GRID count",
    max_grids="Maximum GRID count",
    tag="Filter to decks with this tag",
    channel="Filter to decks from this channel",
  )
  async def run_bulk_cmd(
    self,
    interaction: discord.Interaction,
    repo: str,
    ref: str,
    name: str | None = None,
    sol: str | None = None,
    min_grids: int | None = None,
    max_grids: int | None = None,
    tag: str | None = None,
    channel: discord.TextChannel | None = None,
  ) -> None:
    await interaction.response.defer(thinking=True)

    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if repo not in APPROVED_REPOS:
        allowed = ", ".join(f"`{k}`" for k in APPROVED_REPOS)
        await interaction.followup.send(
          f"`{repo}` is not an approved repository. Allowed: {allowed}",
          ephemeral=True,
        )
        return

      sol_filter: SolType | None = None
      if sol is not None:
        if sol == "other":
          sol_filter = SolType.unknown
        else:
          sol_filter = normalize_sol(sol)
          if sol_filter is None or sol_filter == SolType.unknown:
            await interaction.followup.send(
              f"`{sol}` is not a recognised SOL type. "
              'Use "other" to match unrecognised SOLs.',
              ephemeral=True,
            )
            return

      channel_id = channel.id if channel else None

      # Resolve ref before doing anything else.
      try:
        commit_hash = await resolve_ref(repo, ref)
      except ResolveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

      version = await get_or_create_version(
        session,
        repo,
        commit_hash,
        ref_name=ref if ref != commit_hash else None,
      )
      await session.flush()

      # Fetch all matching decks (no pagination — we want the full list).
      from sqlalchemy import select as sa_select

      from deckbot.db.models import Deck, DeckTag

      query = sa_select(Deck.id)
      if name is not None:
        query = query.where(Deck.filename.ilike(f"%{name}%"))
      if sol_filter is not None:
        query = query.where(Deck.sol == sol_filter.value)
      if min_grids is not None:
        query = query.where(Deck.grid_count >= min_grids)
      if max_grids is not None:
        query = query.where(Deck.grid_count <= max_grids)
      if tag is not None:
        query = query.where(
          Deck.id.in_(sa_select(DeckTag.deck_id).where(DeckTag.tag == tag))
        )
      if channel_id is not None:
        query = query.where(Deck.source_channel_id == channel_id)

      result = await session.execute(query)
      all_deck_ids: list[int] = [row[0] for row in result]

    total = len(all_deck_ids)
    if total == 0:
      await interaction.followup.send(
        "No decks match those filters.", ephemeral=ephemeral
      )
      return

    # Confirmation gate: if ≥50 decks, require the user to confirm via a button.
    if total >= 50:
      confirmed = await _confirm_bulk(interaction, total, repo, ref, ephemeral)
      if not confirmed:
        return

    # Enqueue runs, skipping decks that already have a pending/running run.
    queued = 0
    skipped = 0
    async with get_session() as session:
      version = await get_or_create_version(
        session,
        repo,
        commit_hash,
        ref_name=ref if ref != commit_hash else None,
      )
      await session.flush()

      for did in all_deck_ids:
        active = await get_active_run_for_deck_version(
          session, did, version.id
        )
        if active is not None:
          skipped += 1
          continue
        session.add(
          Run(
            deck_id=did,
            version_id=version.id,
            status="pending",
            submitted_by=interaction.user.id,
            created_at=datetime.now(UTC),
          )
        )
        queued += 1

      await session.commit()

    ref_display = ref if ref == commit_hash else f"{ref} ({commit_hash[:8]})"
    parts = [f"Queued **{queued}** run(s) on `{repo}@{ref_display}`."]
    if skipped:
      parts.append(
        f"{skipped} deck(s) already had a pending/running run and were skipped."
      )
    await interaction.followup.send(" ".join(parts), ephemeral=ephemeral)

  @run_bulk_cmd.autocomplete("repo")
  async def _run_bulk_repo_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=k, value=k)
      for k in APPROVED_REPOS
      if current.lower() in k.lower()
    ][:25]

  @run_bulk_cmd.autocomplete("sol")
  async def _run_bulk_sol_autocomplete(
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

  @run_bulk_cmd.autocomplete("tag")
  async def _run_bulk_tag_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=t, value=t)
      for t in _TAGS
      if current.lower() in t.lower()
    ]

  # ── /deck repos ───────────────────────────────────────────────────────────

  @deck.command(
    name="repos",
    description="List approved MYSTRAN repositories for running decks",
  )
  async def repos_cmd(self, interaction: discord.Interaction) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

    lines = [f"`{name}` — <{url}>" for name, url in APPROVED_REPOS.items()]
    embed = discord.Embed(
      title="Approved MYSTRAN Repositories",
      description="\n".join(lines) if lines else "_none_",
      colour=discord.Colour.orange(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

  # ── /deck runs ────────────────────────────────────────────────────────────

  @deck.command(
    name="runs",
    description="List MYSTRAN runs for a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID",
    page="Page number (default: 1)",
  )
  async def runs_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int,
    page: int = 1,
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

      runs, total = await list_runs_for_deck(
        session, deck_id, page=page, per_page=5
      )

    if not runs:
      await interaction.response.send_message(
        f"No runs recorded for deck `#{deck_id}` yet.", ephemeral=ephemeral
      )
      return

    total_pages = max(1, math.ceil(total / 5))
    embed = discord.Embed(
      title=f"Runs for deck #{deck_id} — page {page}/{total_pages} (total: {total})",
      colour=discord.Colour.orange(),
    )
    for run in runs:
      _add_run_field(embed, run)

    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

  # ── /deck run-status ──────────────────────────────────────────────────────

  @deck.command(
    name="run-status",
    description="Show details for a single MYSTRAN run",
  )
  @app_commands.describe(run_id="Run ID")
  async def run_status_cmd(
    self,
    interaction: discord.Interaction,
    run_id: int,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
      run = await get_run(session, run_id)

    if run is None:
      await interaction.response.send_message(
        f"No run with ID `{run_id}`.", ephemeral=True
      )
      return

    embed = discord.Embed(
      title=f"Run #{run_id}",
      colour=_run_colour(run.status),
    )
    embed.add_field(
      name="Deck",
      value=f"#{run.deck_id} `{run.deck.filename}`",
      inline=True,
    )
    embed.add_field(
      name="Version",
      value=(
        f"`{run.version.repo_name}@"
        f"{run.version.ref_name or run.version.commit_hash[:8]}`"
      ),
      inline=True,
    )
    embed.add_field(
      name="Status",
      value=f"{_RUN_EMOJI.get(run.status, '❓')} `{run.status}`",
      inline=True,
    )
    if run.node:
      embed.add_field(name="Node", value=f"`{run.node.name}`", inline=True)
    if run.exit_code is not None:
      embed.add_field(
        name="Exit code", value=f"`{run.exit_code}`", inline=True
      )
    embed.add_field(
      name="Queued",
      value=run.created_at.strftime("%Y-%m-%d %H:%M UTC"),
      inline=True,
    )
    if run.started_at:
      embed.add_field(
        name="Started",
        value=run.started_at.strftime("%Y-%m-%d %H:%M UTC"),
        inline=True,
      )
    if run.completed_at:
      embed.add_field(
        name="Completed",
        value=run.completed_at.strftime("%Y-%m-%d %H:%M UTC"),
        inline=True,
      )
    if run.error:
      short = run.error[:300]
      if len(run.error) > 300:
        short += "…"
      embed.add_field(
        name="Error / notes", value=f"```{short}```", inline=False
      )

    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# ── Run formatting helpers ────────────────────────────────────────────────────

_RUN_EMOJI: dict[str, str] = {
  "pending": "⏳",
  "running": "🔄",
  "completed": "✅",
  "failed": "❌",
  "cancelled": "🚫",
}


def _run_colour(status: str) -> discord.Colour:
  return {
    "completed": discord.Colour.green(),
    "failed": discord.Colour.red(),
    "running": discord.Colour.yellow(),
    "cancelled": discord.Colour.greyple(),
  }.get(status, discord.Colour.orange())


def _add_run_field(embed: discord.Embed, run: Run) -> None:
  emoji = _RUN_EMOJI.get(run.status, "❓")
  ref = run.version.ref_name or run.version.commit_hash[:8]
  name = f"#{run.id} {emoji} `{run.version.repo_name}@{ref}`"
  parts = [f"status: `{run.status}`"]
  if run.node:
    parts.append(f"node: `{run.node.name}`")
  if run.exit_code is not None:
    parts.append(f"exit: `{run.exit_code}`")
  parts.append(run.created_at.strftime("%Y-%m-%d %H:%M UTC"))
  embed.add_field(name=name, value=" · ".join(parts), inline=False)


# ── Bulk-run confirmation view ────────────────────────────────────────────────


class _BulkConfirmView(discord.ui.View):
  def __init__(self) -> None:
    super().__init__(timeout=60.0)
    self.confirmed: bool | None = None

  @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    self.confirmed = True
    self.stop()
    await interaction.response.defer()

  @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
  async def cancel(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    self.confirmed = False
    self.stop()
    await interaction.response.defer()


async def _confirm_bulk(
  interaction: discord.Interaction,
  total: int,
  repo: str,
  ref: str,
  ephemeral: bool,
) -> bool:
  """Send a confirmation prompt and return True if the user confirms."""
  view = _BulkConfirmView()
  await interaction.followup.send(
    f"This will queue **{total}** runs on `{repo}@{ref}`. Are you sure?",
    view=view,
    ephemeral=ephemeral,
  )
  await view.wait()
  if view.confirmed:
    return True
  await interaction.followup.send("Cancelled.", ephemeral=ephemeral)
  return False


async def setup(bot: commands.Bot) -> None:
  await bot.add_cog(DecksCog(bot))  # type: ignore[arg-type]
