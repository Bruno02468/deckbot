from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from deckbot.config import get_settings
from deckbot.db.models import Deck, DeckTag, Run, RunBatch
from deckbot.db.queries import (
  BatchSummary,
  cancel_batch_runs,
  create_batch,
  get_active_run_for_deck_version,
  get_any_run_for_deck_version,
  get_batch,
  get_batch_summary,
  get_deck,
  get_deckbot_channel_id,
  get_decks_by_hashes,
  get_decks_by_message,
  get_or_create_version,
  get_node_by_name,
  get_run,
  list_recent_batches,
  list_runs_for_batch,
  list_runs_for_deck,
  search_runs,
)
from deckbot.db.session import get_session
from deckbot.models.deck import DeckInfo  # noqa: F401
from deckbot.models.repo import APPROVED_REPOS
from deckbot.models.sol import SolType, normalize_sol
from deckbot.services.deck_parser import hash_deck
from deckbot.services.version_resolver import ResolveError, resolve_ref
from deckbot.services.zip_handler import DECK_EXTENSIONS, extract_decks

if TYPE_CHECKING:
  from sqlalchemy.ext.asyncio import AsyncSession

  from deckbot.bot import DeckBot

log = logging.getLogger(__name__)

_VIEW_TIMEOUT = 120.0
RUNS_PER_BATCH_PAGE = 10

# Predefined tag values (mirrored from decks.py for autocomplete).
_TAGS: list[str] = [
  "should_fatal",
  "incompatible",
  "bad_result",
  "slow",
  "big",
  "memcheck_set",
]

_RUN_EMOJI: dict[str, str] = {
  "pending": "⏳",
  "building": "🔨",
  "running": "🔄",
  "completed": "✅",
  "failed": "❌",
  "cancelled": "🚫",
}

_FINISH_EMOJI: dict[str, str] = {
  "normal": "✅",
  "fatal": "⚠️",
  "crash": "💥",
}

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _ts(dt: datetime, fmt: str = "f") -> str:
  """Format a UTC-aware datetime as a Discord timestamp tag."""
  return f"<t:{int(dt.timestamp())}:{fmt}>"


def _is_ephemeral(
  interaction: discord.Interaction, deckbot_channel_id: int | None
) -> bool:
  return (
    deckbot_channel_id is None or interaction.channel_id != deckbot_channel_id
  )


def _run_colour(status: str) -> discord.Colour:
  return {
    "completed": discord.Colour.green(),
    "failed": discord.Colour.red(),
    "building": discord.Colour.blue(),
    "running": discord.Colour.yellow(),
    "cancelled": discord.Colour.greyple(),
  }.get(status, discord.Colour.orange())


def _fmt_elapsed(delta: timedelta) -> str:
  total = int(delta.total_seconds())
  if total < 0:
    total = 0
  h, rem = divmod(total, 3600)
  m, s = divmod(rem, 60)
  if h:
    return f"{h}h {m}m {s}s"
  if m:
    return f"{m}m {s}s"
  return f"{s}s"


def _build_run_embed(run: Run, api_public_url: str | None) -> discord.Embed:
  """Build the canonical embed for a single run."""
  embed = discord.Embed(
    title=f"Run #{run.id}",
    colour=_run_colour(run.status),
  )

  # ── Row 1: Deck · Version · Status ───────────────────────────────────────
  embed.add_field(
    name="Deck",
    value=f"#{run.deck_id} `{run.deck.filename}`",
    inline=True,
  )
  ref = run.version.ref_name or run.version.commit_hash[:8]
  version_value = f"`{run.version.repo_name}@{ref}`"
  if run.version.ref_name:
    version_value += f"\n`{run.version.commit_hash[:8]}`"
  embed.add_field(
    name="Version",
    value=version_value,
    inline=True,
  )
  embed.add_field(
    name="Status",
    value=f"{_RUN_EMOJI.get(run.status, '❓')} `{run.status}`",
    inline=True,
  )

  # ── Row 2: Queued · Completed/Started/— · Elapsed/Running for/Waiting ────
  now = datetime.now(UTC)
  created = run.created_at.replace(tzinfo=UTC)
  embed.add_field(
    name="Queued",
    value=_ts(created),
    inline=True,
  )

  if run.status == "pending":
    embed.add_field(name="Completed", value="—", inline=True)
    embed.add_field(
      name="Waiting", value=_fmt_elapsed(now - created), inline=True
    )
  elif run.status == "building" and run.started_at:
    build_started = run.started_at.replace(tzinfo=UTC)
    embed.add_field(
      name="Build started",
      value=_ts(build_started),
      inline=True,
    )
    embed.add_field(
      name="Building for",
      value=_fmt_elapsed(now - build_started),
      inline=True,
    )
  elif run.status == "running":
    run_started = (
      run.run_started_at.replace(tzinfo=UTC)
      if run.run_started_at
      else run.started_at.replace(tzinfo=UTC)
      if run.started_at
      else None
    )
    if run_started:
      embed.add_field(
        name="Started",
        value=_ts(run_started),
        inline=True,
      )
      embed.add_field(
        name="Running for",
        value=_fmt_elapsed(now - run_started),
        inline=True,
      )
    else:
      embed.add_field(name="Completed", value="—", inline=True)
      embed.add_field(name="Elapsed", value="—", inline=True)
  elif run.completed_at:
    embed.add_field(
      name="Completed",
      value=_ts(run.completed_at.replace(tzinfo=UTC)),
      inline=True,
    )
    exec_start = (
      run.run_started_at.replace(tzinfo=UTC)
      if run.run_started_at
      else run.started_at.replace(tzinfo=UTC)
      if run.started_at
      else None
    )
    if exec_start:
      elapsed = _fmt_elapsed(run.completed_at.replace(tzinfo=UTC) - exec_start)
      embed.add_field(name="Elapsed", value=elapsed, inline=True)
    else:
      embed.add_field(name="Elapsed", value="—", inline=True)
  else:
    embed.add_field(name="Completed", value="—", inline=True)
    embed.add_field(name="Elapsed", value="—", inline=True)

  # ── Row 3 (completed): Output files · Finish · Valgrind ──────────────────
  if run.status == "completed":
    run_files = getattr(run, "files", []) or []
    links: list[str] = []
    if api_public_url:
      base = api_public_url.rstrip("/")
      f06 = next(
        (f for f in run_files if f.filename.lower().endswith(".f06")), None
      )
      op2 = next(
        (f for f in run_files if f.filename.lower().endswith(".op2")), None
      )
      if f06:
        links.append(f"[F06]({base}/run/{run.id}/files/{f06.filename})")
      if op2:
        links.append(f"[OP2]({base}/run/{run.id}/files/{op2.filename})")
      if run_files:
        links.append(f"[everything]({base}/run/{run.id}/zip)")
    embed.add_field(
      name="Output files",
      value=" · ".join(links) if links else "—",
      inline=True,
    )

    finish = getattr(run, "finish", None)
    embed.add_field(
      name="Finish",
      value=(
        f"{_FINISH_EMOJI.get(finish, '❓')} `{finish}`"
        if finish is not None
        else "—"
      ),
      inline=True,
    )

    verrs = getattr(run, "valgrind_errors", None)
    embed.add_field(
      name="Valgrind",
      value=(
        f"{'🧹' if verrs == 0 else '🐛'} `{verrs} error(s)`"
        if verrs is not None
        else "—"
      ),
      inline=True,
    )

  if run.error:
    short = run.error[:300]
    if len(run.error) > 300:
      short += "…"
    embed.add_field(name="Error / notes", value=f"```{short}```", inline=False)

  return embed


def _add_run_field(embed: discord.Embed, run: Run) -> None:
  emoji = _RUN_EMOJI.get(run.status, "❓")
  ref = run.version.ref_name or run.version.commit_hash[:8]
  name = f"#{run.id} {emoji} `{run.version.repo_name}@{ref}`"
  parts = [f"status: `{run.status}`"]
  if run.node:
    parts.append(f"node: `{run.node.name}`")

  now = datetime.now(UTC)
  created = run.created_at.replace(tzinfo=UTC)
  if run.status == "pending":
    parts.append(f"waiting {_fmt_elapsed(now - created)}")
  elif run.status == "building" and run.started_at:
    build_started = run.started_at.replace(tzinfo=UTC)
    parts.append(f"building {_fmt_elapsed(now - build_started)}")
  elif run.status == "running":
    run_started = (
      run.run_started_at.replace(tzinfo=UTC)
      if run.run_started_at
      else run.started_at.replace(tzinfo=UTC)
      if run.started_at
      else None
    )
    if run_started:
      parts.append(f"running {_fmt_elapsed(now - run_started)}")
  elif run.completed_at and run.started_at:
    elapsed = _fmt_elapsed(
      run.completed_at.replace(tzinfo=UTC) - run.started_at.replace(tzinfo=UTC)
    )
    parts.append(f"took {elapsed}")

  finish = getattr(run, "finish", None)
  if finish is not None:
    parts.append(f"{_FINISH_EMOJI.get(finish, '❓')} {finish}")
  verrs = getattr(run, "valgrind_errors", None)
  if verrs is not None:
    parts.append(f"{'🧹' if verrs == 0 else '🐛'} {verrs} valgrind error(s)")

  parts.append(_ts(created))
  embed.add_field(name=name, value=" · ".join(parts), inline=False)


# ── Deck-select view (context menu, multi-deck messages) ──────────────────────


class _DeckSelectView(discord.ui.View):
  """Select menu to pick one deck from a multi-deck message, then run it."""

  def __init__(
    self,
    decks: list,  # list[Deck] — avoid circular import typing
    ephemeral: bool,
  ) -> None:
    super().__init__(timeout=60.0)
    self._ephemeral = ephemeral
    self.select.options = [
      discord.SelectOption(
        label=f"#{d.id} {d.filename[:90]}",
        value=str(d.id),
        description=(f"SOL: {d.sol or '—'}  GRIDs: {d.grid_count}"),
      )
      for d in decks[:25]
    ]

  @discord.ui.select(placeholder="Choose a deck…")
  async def select(
    self,
    interaction: discord.Interaction,
    item: discord.ui.Select[Any],
  ) -> None:
    deck_id = int(item.values[0])
    modal = RunDeckModal(ephemeral=self._ephemeral, preset_deck_id=deck_id)
    await interaction.response.send_modal(modal)
    self.stop()


# ── Run submission modal ──────────────────────────────────────────────────────


class RunDeckModal(discord.ui.Modal, title="Run deck with MYSTRAN"):
  """Modal that queues a MYSTRAN run for a chosen deck."""

  deck_id_input = discord.ui.TextInput(
    label="Deck ID",
    placeholder="e.g. 42",
    min_length=1,
    max_length=10,
  )
  ref_input = discord.ui.TextInput(
    label="Branch / tag / commit",
    placeholder="e.g. main",
    min_length=1,
    max_length=100,
  )

  def __init__(
    self,
    ephemeral: bool,
    preset_deck_id: int | None = None,
  ) -> None:
    super().__init__()
    self._ephemeral = ephemeral
    if preset_deck_id is not None:
      self.deck_id_input.default = str(preset_deck_id)

  async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
    await interaction.response.defer(thinking=True, ephemeral=self._ephemeral)
    raw_id = self.deck_id_input.value.strip()
    ref = self.ref_input.value.strip()
    repo = next(iter(APPROVED_REPOS))

    try:
      deck_id = int(raw_id)
    except ValueError:
      await interaction.followup.send(
        f"`{raw_id}` is not a valid deck ID.", ephemeral=True
      )
      return

    async with get_session() as session:
      deck = await get_deck(session, deck_id)
      if deck is None:
        await interaction.followup.send(
          f"No deck with ID `{deck_id}`.", ephemeral=True
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

      existing = await get_any_run_for_deck_version(
        session, deck_id, version.id
      )
      if existing is not None:
        api_public_url = get_settings().api_public_url
        embed = _build_run_embed(existing, api_public_url)
        embed.set_footer(
          text=f"Cached — run #{existing.id} already exists for this "
          "deck+version. Use Force Re-run to queue a new one."
        )
        view = _CachedRunView(
          deck_id, version.id, self._ephemeral, api_public_url
        )
        await interaction.followup.send(
          embed=embed, view=view, ephemeral=self._ephemeral
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
      run = await get_run(session, run_id)
      assert run is not None

    api_public_url = get_settings().api_public_url
    view = RunStatusView(run_id, api_public_url)
    embed = _build_run_embed(run, api_public_url)
    msg = await interaction.followup.send(
      embed=embed, view=view, ephemeral=self._ephemeral, wait=True
    )
    asyncio.get_event_loop().create_task(
      _auto_update_run(msg, run_id, view, api_public_url)
    )


# ── Run-status live view ──────────────────────────────────────────────────────


class RunStatusView(discord.ui.View):
  """A persistent embed for a single run with a manual refresh button."""

  def __init__(self, run_id: int, api_public_url: str | None) -> None:
    super().__init__(timeout=None)
    self._run_id = run_id
    self._api_public_url = api_public_url

  @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
  async def refresh(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    async with get_session() as session:
      run = await get_run(session, self._run_id)
    if run is None:
      await interaction.response.send_message("Run not found.", ephemeral=True)
      return
    embed = _build_run_embed(run, self._api_public_url)
    done = run.status in _TERMINAL_STATUSES
    await interaction.response.edit_message(
      embed=embed, view=None if done else self
    )


# ── Cached-run view ───────────────────────────────────────────────────────────


class _CachedRunView(discord.ui.View):
  """Shown when an existing run is found; offers a Force Re-run button."""

  def __init__(
    self,
    deck_id: int,
    version_id: int,
    ephemeral: bool,
    api_public_url: str | None,
  ) -> None:
    super().__init__(timeout=_VIEW_TIMEOUT)
    self._deck_id = deck_id
    self._version_id = version_id
    self._ephemeral = ephemeral
    self._api_public_url = api_public_url

  @discord.ui.button(label="🔁 Force Re-run", style=discord.ButtonStyle.danger)
  async def force_rerun(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    button.disabled = True
    await interaction.response.defer()
    async with get_session() as session:
      run = Run(
        deck_id=self._deck_id,
        version_id=self._version_id,
        status="pending",
        submitted_by=interaction.user.id,
        created_at=datetime.now(UTC),
      )
      session.add(run)
      await session.commit()
      run_id = run.id
      run = await get_run(session, run_id)
      assert run is not None
    self.stop()
    new_view = RunStatusView(run_id, self._api_public_url)
    embed = _build_run_embed(run, self._api_public_url)
    msg = await interaction.edit_original_response(embed=embed, view=new_view)
    asyncio.get_event_loop().create_task(
      _auto_update_run(msg, run_id, new_view, self._api_public_url)
    )


async def _auto_update_run(
  message: discord.Message,
  run_id: int,
  view: RunStatusView,
  api_public_url: str | None,
) -> None:
  """Background task: edit *message* with fresh run status on a schedule.

  Schedule: every 10 s for the first minute, then every 60 s for 10 minutes.
  Stops early when the run reaches a terminal state.
  """
  schedule = [10] * 6 + [60] * 10
  for delay in schedule:
    await asyncio.sleep(delay)
    try:
      async with get_session() as session:
        run = await get_run(session, run_id)
      if run is None:
        break
      embed = _build_run_embed(run, api_public_url)
      done = run.status in _TERMINAL_STATUSES
      await message.edit(embed=embed, view=None if done else view)
      if done:
        break
    except (discord.HTTPException, asyncio.CancelledError):
      break


# ── Batch embed builders ──────────────────────────────────────────────────────


def _batch_colour(summary: BatchSummary) -> discord.Colour:
  active = sum(
    summary.by_status.get(s, 0) for s in ("pending", "building", "running")
  )
  if active > 0:
    return discord.Colour.orange()
  if summary.by_status.get("failed", 0) > 0 or summary.infra_errors > 0:
    return discord.Colour.red()
  return discord.Colour.green()


def _build_batch_summary_embed(
  batch: RunBatch,
  summary: BatchSummary,
) -> discord.Embed:
  label_suffix = f" — {batch.label}" if batch.label else ""
  embed = discord.Embed(
    title=f"Batch #{batch.id}{label_suffix}",
    colour=_batch_colour(summary),
  )
  ref = batch.version.ref_name or batch.version.commit_hash[:8]
  version_value = f"`{batch.version.repo_name}@{ref}`"
  if batch.version.ref_name:
    version_value += f"\n`{batch.version.commit_hash[:8]}`"
  embed.add_field(name="Version", value=version_value, inline=True)
  embed.add_field(
    name="Submitted by", value=f"<@{batch.submitted_by}>", inline=True
  )
  embed.add_field(
    name="Created",
    value=_ts(batch.created_at.replace(tzinfo=UTC)),
    inline=True,
  )
  if batch.filter_summary:
    try:
      filters: dict[str, Any] = json.loads(batch.filter_summary)
      parts = [f"`{k}`: {v}" for k, v in filters.items() if v is not None]
      if parts:
        embed.add_field(name="Filters", value=" · ".join(parts), inline=False)
    except (ValueError, TypeError):
      pass
  status_order = [
    "pending",
    "building",
    "running",
    "completed",
    "failed",
    "cancelled",
  ]
  status_parts = [
    f"{_RUN_EMOJI.get(s, '')} {s}: **{n}**"
    for s in status_order
    if (n := summary.by_status.get(s, 0))
  ]
  embed.add_field(
    name=f"Runs ({summary.total} total)",
    value=" · ".join(status_parts) if status_parts else "—",
    inline=False,
  )
  completed = summary.by_status.get("completed", 0)
  if completed:
    finish_parts = [
      f"{emoji} {fv}: **{n}**"
      for fv, emoji in _FINISH_EMOJI.items()
      if (n := summary.by_finish.get(fv, 0))
    ]
    if summary.by_finish.get("unknown", 0):
      finish_parts.append(f"❓ unknown: **{summary.by_finish['unknown']}**")
    embed.add_field(
      name="Finish",
      value=" · ".join(finish_parts) if finish_parts else "—",
      inline=True,
    )
    vg_parts: list[str] = []
    if summary.valgrind_clean:
      vg_parts.append(f"🧹 clean: **{summary.valgrind_clean}**")
    if summary.valgrind_errors_found:
      vg_parts.append(f"🐛 errors: **{summary.valgrind_errors_found}**")
    if summary.valgrind_no_data:
      vg_parts.append(f"— no data: **{summary.valgrind_no_data}**")
    embed.add_field(
      name="Valgrind",
      value=" · ".join(vg_parts) if vg_parts else "—",
      inline=True,
    )
  if summary.infra_errors:
    embed.add_field(
      name="⚠️ Infrastructure errors",
      value=f"{summary.infra_errors} run(s) had node/system errors",
      inline=False,
    )
  return embed


def _build_batch_runs_embed(
  batch: RunBatch,
  runs: list[Run],
  page: int,
  total_pages: int,
) -> discord.Embed:
  label_suffix = f" — {batch.label}" if batch.label else ""
  embed = discord.Embed(
    title=(
      f"Batch #{batch.id}{label_suffix} — Runs (page {page}/{total_pages})"
    ),
    colour=discord.Colour.orange(),
  )
  for run in runs:
    _add_run_field(embed, run)
  return embed


# ── Batch views ───────────────────────────────────────────────────────────────


class _RunSelect(discord.ui.Select):
  """Dropdown to view a single run's detail from a batch run-list page."""

  def __init__(
    self,
    runs: list[Run],
    api_public_url: str | None,
  ) -> None:
    self._api_public_url = api_public_url
    options = [
      discord.SelectOption(
        label=f"#{r.id} {r.deck.filename[:80]}",
        value=str(r.id),
        description=(
          f"{_RUN_EMOJI.get(r.status, '?')} {r.status}"
          + (f" · {r.finish}" if getattr(r, "finish", None) else "")
        ),
      )
      for r in runs
    ]
    super().__init__(
      placeholder="View a run's details…",
      options=options,
      row=2,
    )

  async def callback(self, interaction: discord.Interaction) -> None:
    run_id = int(self.values[0])
    async with get_session() as session:
      run = await get_run(session, run_id)
    if run is None:
      await interaction.response.send_message("Run not found.", ephemeral=True)
      return
    embed = _build_run_embed(run, self._api_public_url)
    done = run.status in _TERMINAL_STATUSES
    view = None if done else RunStatusView(run_id, self._api_public_url)
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=True
    )


class BatchView(discord.ui.View):
  """Batch summary (page=0) or paginated run list (page≥1).

  Page 0 buttons: 🔄 Refresh | View Runs ► | ❌ Cancel Batch
  Page ≥1 buttons: ◄ Summary | ◄ Prev | Next ► | 🔄 Refresh
  Page ≥1 also has a run-select dropdown (row 1).
  """

  def __init__(
    self,
    batch_id: int,
    api_public_url: str | None,
    ephemeral: bool,
    page: int = 0,
    total_run_pages: int = 1,
  ) -> None:
    super().__init__(timeout=None)
    self._batch_id = batch_id
    self._api_public_url = api_public_url
    self._ephemeral = ephemeral
    self._page = page
    self._total_run_pages = total_run_pages
    if page == 0:
      self.remove_item(self.btn_summary)
      self.remove_item(self.btn_prev)
      self.remove_item(self.btn_next)
      self.remove_item(self.btn_refresh_runs)
    else:
      self.remove_item(self.btn_view_runs)
      self.remove_item(self.btn_cancel)
      self.remove_item(self.btn_refresh_summary)
      self.btn_prev.disabled = page <= 1
      self.btn_next.disabled = page >= total_run_pages

  async def _go_to_page(
    self, interaction: discord.Interaction, new_page: int
  ) -> None:
    async with get_session() as session:
      batch = await get_batch(session, self._batch_id)
      if batch is None:
        await interaction.response.send_message(
          "Batch not found.", ephemeral=True
        )
        return
      if new_page == 0:
        summary = await get_batch_summary(session, self._batch_id)
        total_run_pages = max(
          1, math.ceil(summary.total / RUNS_PER_BATCH_PAGE)
        )
        embed = _build_batch_summary_embed(batch, summary)
        new_view: BatchView = BatchView(
          self._batch_id,
          self._api_public_url,
          self._ephemeral,
          page=0,
          total_run_pages=total_run_pages,
        )
      else:
        runs, total = await list_runs_for_batch(
          session, self._batch_id, page=new_page
        )
        total_run_pages = max(1, math.ceil(total / RUNS_PER_BATCH_PAGE))
        embed = _build_batch_runs_embed(batch, runs, new_page, total_run_pages)
        new_view = BatchView(
          self._batch_id,
          self._api_public_url,
          self._ephemeral,
          page=new_page,
          total_run_pages=total_run_pages,
        )
        if runs:
          new_view.add_item(_RunSelect(runs, self._api_public_url))
    await interaction.response.edit_message(embed=embed, view=new_view)

  @discord.ui.button(
    label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=0
  )
  async def btn_refresh_summary(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, 0)

  @discord.ui.button(
    label="View Runs ►", style=discord.ButtonStyle.primary, row=0
  )
  async def btn_view_runs(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, 1)

  @discord.ui.button(
    label="❌ Cancel Batch", style=discord.ButtonStyle.danger, row=0
  )
  async def btn_cancel(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    confirm_view = _BatchCancelConfirmView(self._batch_id)
    await interaction.response.send_message(
      f"Cancel all pending/building runs in batch #{self._batch_id}?",
      view=confirm_view,
      ephemeral=True,
    )

  @discord.ui.button(
    label="◄ Summary", style=discord.ButtonStyle.secondary, row=1
  )
  async def btn_summary(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, 0)

  @discord.ui.button(
    label="◄ Prev", style=discord.ButtonStyle.secondary, row=1
  )
  async def btn_prev(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, self._page - 1)

  @discord.ui.button(
    label="Next ►", style=discord.ButtonStyle.secondary, row=1
  )
  async def btn_next(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, self._page + 1)

  @discord.ui.button(
    label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=1
  )
  async def btn_refresh_runs(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to_page(interaction, self._page)


class _BatchCancelConfirmView(discord.ui.View):
  """Ephemeral confirmation before cancelling all active runs in a batch."""

  def __init__(self, batch_id: int) -> None:
    super().__init__(timeout=60.0)
    self._batch_id = batch_id

  @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
  async def confirm(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    button.disabled = True
    await interaction.response.defer()
    async with get_session() as session:
      count = await cancel_batch_runs(session, self._batch_id)
      await session.commit()
    self.stop()
    await interaction.edit_original_response(
      content=f"Cancelled **{count}** run(s) in batch #{self._batch_id}.",
      view=None,
    )

  @discord.ui.button(label="Abort", style=discord.ButtonStyle.secondary)
  async def abort(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    self.stop()
    await interaction.response.edit_message(
      content="Cancellation aborted.", view=None
    )


async def _auto_update_batch(
  message: discord.Message,
  batch_id: int,
  api_public_url: str | None,
  ephemeral: bool,
) -> None:
  """Background task: keep the batch summary message up-to-date.

  Polls every 30 s for up to 30 minutes, stopping early when all runs
  reach a terminal state.  Always renders page 0 (summary).
  """
  for _ in range(60):  # 60 × 30 s = 30 min max
    await asyncio.sleep(30)
    try:
      async with get_session() as session:
        batch = await get_batch(session, batch_id)
        if batch is None:
          break
        summary = await get_batch_summary(session, batch_id)
      active = sum(
        summary.by_status.get(s, 0) for s in ("pending", "building", "running")
      )
      total_run_pages = max(1, math.ceil(summary.total / RUNS_PER_BATCH_PAGE))
      embed = _build_batch_summary_embed(batch, summary)
      view = BatchView(
        batch_id,
        api_public_url,
        ephemeral,
        page=0,
        total_run_pages=total_run_pages,
      )
      await message.edit(embed=embed, view=view)
      if active == 0:
        break
    except (discord.HTTPException, asyncio.CancelledError):
      break


# ── Batch list view ───────────────────────────────────────────────────────────


class _BatchListView(discord.ui.View):
  """One 'View →' button per batch row returned by /run batches."""

  def __init__(
    self,
    batches: list[RunBatch],
    api_public_url: str | None,
    ephemeral: bool,
  ) -> None:
    super().__init__(timeout=_VIEW_TIMEOUT)
    for batch in batches[:5]:
      label_suffix = f" — {batch.label[:30]}" if batch.label else ""
      btn: discord.ui.Button[Any] = discord.ui.Button(
        label=f"#{batch.id}{label_suffix}",
        style=discord.ButtonStyle.primary,
      )
      btn.callback = _make_batch_btn_cb(batch.id, api_public_url, ephemeral)
      self.add_item(btn)


def _make_batch_btn_cb(
  batch_id: int,
  api_public_url: str | None,
  ephemeral: bool,
) -> Any:
  async def cb(interaction: discord.Interaction) -> None:
    async with get_session() as session:
      batch = await get_batch(session, batch_id)
      if batch is None:
        await interaction.response.send_message(
          "Batch not found.", ephemeral=True
        )
        return
      summary = await get_batch_summary(session, batch_id)
    total_run_pages = max(1, math.ceil(summary.total / RUNS_PER_BATCH_PAGE))
    embed = _build_batch_summary_embed(batch, summary)
    view = BatchView(
      batch_id,
      api_public_url,
      ephemeral,
      page=0,
      total_run_pages=total_run_pages,
    )
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=True
    )

  return cb


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


# ── Helper: resolve decks from a Discord message ──────────────────────────────


async def _resolve_decks_from_message(
  message: discord.Message,
  session: AsyncSession,
) -> list[Deck]:
  """Find stored decks referenced by a message."""
  decks = await get_decks_by_message(session, message.id)
  if decks:
    return decks

  hashes: list[str] = []
  for attachment in message.attachments:
    ext = PurePosixPath(attachment.filename).suffix.lower()
    try:
      data = await attachment.read()
    except discord.HTTPException:
      continue
    if ext == ".zip":
      for _, raw in extract_decks(data):
        hashes.append(hash_deck(raw))
    elif ext in DECK_EXTENSIONS:
      hashes.append(hash_deck(data))

  if not hashes:
    return []
  return await get_decks_by_hashes(session, hashes)


# ── Run search helpers ────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _parse_duration(s: str) -> int | None:
  """Parse a human duration string to total seconds.

  Accepts: ``30s``, ``5m``, ``2h``, ``1h30m``, ``1h30m45s``.
  Returns ``None`` if the string is blank or doesn't match.
  """
  m = _DURATION_RE.fullmatch(s.strip().lower())
  if m is None or not any(m.groups()):
    return None
  h = int(m.group(1) or 0)
  mins = int(m.group(2) or 0)
  secs = int(m.group(3) or 0)
  return h * 3600 + mins * 60 + secs


@dataclass
class RunSearchParams:
  deck_id: int | None = None
  deck_name: str | None = None
  status: str | None = None
  finish: str | None = None
  node: str | None = None
  node_id: int | None = None
  batch_id: int | None = None
  submitted_by_id: int | None = None
  submitted_by_name: str | None = None
  min_elapsed_s: int | None = None
  max_elapsed_s: int | None = None
  min_elapsed_label: str | None = None
  max_elapsed_label: str | None = None
  valgrind: str | None = None
  sort_by: str = "newest"

  def filter_summary(self) -> str:
    parts: list[str] = []
    if self.deck_id is not None:
      parts.append(f"deck #{self.deck_id}")
    elif self.deck_name is not None:
      parts.append(f"deck~{self.deck_name!r}")
    if self.status is not None:
      parts.append(f"status={self.status}")
    if self.finish is not None:
      parts.append(f"finish={self.finish}")
    if self.node is not None:
      parts.append(f"node={self.node}")
    if self.batch_id is not None:
      parts.append(f"batch #{self.batch_id}")
    if self.submitted_by_name is not None:
      parts.append(f"by {self.submitted_by_name}")
    if self.min_elapsed_label is not None:
      parts.append(f"elapsed≥{self.min_elapsed_label}")
    if self.max_elapsed_label is not None:
      parts.append(f"elapsed≤{self.max_elapsed_label}")
    if self.valgrind is not None:
      parts.append(f"valgrind={self.valgrind}")
    return ", ".join(parts) if parts else "all runs"


# ── Run search view ───────────────────────────────────────────────────────────


class RunSearchView(discord.ui.View):
  """Paginated view for /run search results."""

  def __init__(
    self,
    params: RunSearchParams,
    runs: list[Run],
    total: int,
    page: int,
    ephemeral: bool,
    invoker_id: int,
  ) -> None:
    super().__init__(timeout=_VIEW_TIMEOUT)
    self.message: discord.Message | None = None
    self._params = params
    self._runs = runs
    self._total = total
    self._page = page
    self._ephemeral = ephemeral
    self._invoker_id = invoker_id
    self._per_page = RUNS_PER_BATCH_PAGE
    self._update_buttons()

  def _total_pages(self) -> int:
    return max(1, math.ceil(self._total / self._per_page))

  def _update_buttons(self) -> None:
    self.prev_button.disabled = self._page <= 1
    self.next_button.disabled = self._page >= self._total_pages()

  def _build_embed(self) -> discord.Embed:
    sort_label = {
      "newest": "newest first",
      "oldest": "oldest first",
      "longest": "longest elapsed",
      "shortest": "shortest elapsed",
    }.get(self._params.sort_by, self._params.sort_by)
    embed = discord.Embed(
      title=(
        f"Run search — {self._params.filter_summary()} "
        f"[{sort_label}] — "
        f"page {self._page}/{self._total_pages()} (total: {self._total})"
      ),
      colour=discord.Colour.blurple(),
    )
    for r in self._runs:
      _add_run_field(embed, r)
    return embed

  async def _go_to(
    self, interaction: discord.Interaction, new_page: int
  ) -> None:
    if interaction.user.id != self._invoker_id:
      await interaction.response.send_message(
        "These buttons belong to someone else's search.", ephemeral=True
      )
      return
    async with get_session() as session:
      runs, total = await search_runs(
        session,
        deck_id=self._params.deck_id,
        deck_name=self._params.deck_name,
        status=self._params.status,
        finish=self._params.finish,
        node_id=self._params.node_id,
        batch_id=self._params.batch_id,
        submitted_by=self._params.submitted_by_id,
        min_elapsed_s=self._params.min_elapsed_s,
        max_elapsed_s=self._params.max_elapsed_s,
        valgrind=self._params.valgrind,
        sort_by=self._params.sort_by,
        page=new_page,
        per_page=self._per_page,
      )
    self._runs = runs
    self._total = total
    self._page = new_page
    self._update_buttons()
    await interaction.response.edit_message(
      embed=self._build_embed(), view=self
    )

  @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
  async def prev_button(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to(interaction, self._page - 1)

  @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
  async def next_button(
    self,
    interaction: discord.Interaction,
    button: discord.ui.Button[Any],
  ) -> None:
    await self._go_to(interaction, self._page + 1)

  async def on_timeout(self) -> None:
    self.prev_button.disabled = True
    self.next_button.disabled = True
    if self.message is not None:
      try:
        await self.message.edit(view=self)
      except discord.HTTPException:
        pass


# ── RunsCog ───────────────────────────────────────────────────────────────────


class RunsCog(commands.Cog, name="Runs"):
  run = app_commands.Group(
    name="run",
    description="MYSTRAN run management",
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
      log.exception("Unhandled error in RunsCog command", exc_info=error)

  # ── /run run ──────────────────────────────────────────────────────────────

  @run.command(
    name="run",
    description="Queue a MYSTRAN run for a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID to run",
    ref="Branch, tag, or full commit SHA",
  )
  async def run_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int,
    ref: str,
  ) -> None:
    await interaction.response.defer(thinking=True)
    repo = next(iter(APPROVED_REPOS))
    api_public_url = get_settings().api_public_url

    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      deck = await get_deck(session, deck_id)
      if deck is None:
        await interaction.followup.send(
          f"No deck with ID `{deck_id}`.", ephemeral=True
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

      existing = await get_any_run_for_deck_version(
        session, deck_id, version.id
      )
      if existing is not None:
        embed = _build_run_embed(existing, api_public_url)
        embed.set_footer(
          text=f"Cached — run #{existing.id} already exists for this "
          "deck+version. Use Force Re-run to queue a new one."
        )
        view = _CachedRunView(deck_id, version.id, ephemeral, api_public_url)
        await interaction.followup.send(
          embed=embed, view=view, ephemeral=ephemeral
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
      run = await get_run(session, run_id)
      assert run is not None

    view = RunStatusView(run_id, api_public_url)
    embed = _build_run_embed(run, api_public_url)
    msg = await interaction.followup.send(
      embed=embed, view=view, ephemeral=ephemeral, wait=True
    )
    asyncio.get_event_loop().create_task(
      _auto_update_run(msg, run_id, view, api_public_url)
    )

  # ── /run bulk ─────────────────────────────────────────────────────────────

  @run.command(
    name="bulk",
    description="Queue MYSTRAN runs for all decks matching optional filters",
  )
  @app_commands.describe(
    ref="Branch, tag, or full commit SHA",
    label="Optional label for this batch (e.g. 'testing main')",
    name="Filename substring filter",
    sol='SOL type filter (use "other" for unrecognised)',
    min_grids="Minimum GRID count",
    max_grids="Maximum GRID count",
    tag="Filter to decks with this tag",
    channel="Filter to decks from this channel",
  )
  async def bulk_cmd(
    self,
    interaction: discord.Interaction,
    ref: str,
    label: str | None = None,
    name: str | None = None,
    sol: str | None = None,
    min_grids: int | None = None,
    max_grids: int | None = None,
    tag: str | None = None,
    channel: discord.TextChannel | None = None,
  ) -> None:
    await interaction.response.defer(thinking=True)
    repo = next(iter(APPROVED_REPOS))

    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

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

      from sqlalchemy import select as sa_select

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

    if total >= 50:
      confirmed = await _confirm_bulk(interaction, total, repo, ref, ephemeral)
      if not confirmed:
        return

    filter_parts: dict[str, str] = {}
    if name:
      filter_parts["name"] = name
    if sol_filter:
      filter_parts["sol"] = sol_filter.value
    if min_grids is not None:
      filter_parts["min_grids"] = str(min_grids)
    if max_grids is not None:
      filter_parts["max_grids"] = str(max_grids)
    if tag:
      filter_parts["tag"] = tag
    if channel_id:
      filter_parts["channel"] = f"<#{channel_id}>"
    filter_summary = json.dumps(filter_parts) if filter_parts else None

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

      batch = await create_batch(
        session,
        version_id=version.id,
        submitted_by=interaction.user.id,
        label=label,
        filter_summary=filter_summary,
      )

      for did in all_deck_ids:
        existing = await get_active_run_for_deck_version(
          session, did, version.id
        )
        if existing is not None:
          skipped += 1
          continue
        session.add(
          Run(
            deck_id=did,
            version_id=version.id,
            batch_id=batch.id,
            status="pending",
            submitted_by=interaction.user.id,
            created_at=datetime.now(UTC),
          )
        )
        queued += 1

      await session.commit()
      batch_id = batch.id

    api_public_url = get_settings().api_public_url
    async with get_session() as session:
      batch = await get_batch(session, batch_id)
      assert batch is not None
      summary = await get_batch_summary(session, batch_id)

    total_run_pages = max(1, math.ceil(summary.total / RUNS_PER_BATCH_PAGE))
    embed = _build_batch_summary_embed(batch, summary)
    view = BatchView(
      batch_id,
      api_public_url,
      ephemeral,
      page=0,
      total_run_pages=total_run_pages,
    )
    if skipped:
      embed.set_footer(
        text=f"{skipped} deck(s) with active runs were skipped."
      )
    msg = await interaction.followup.send(
      embed=embed, view=view, ephemeral=ephemeral, wait=True
    )
    asyncio.get_event_loop().create_task(
      _auto_update_batch(msg, batch_id, api_public_url, ephemeral)
    )

  @bulk_cmd.autocomplete("sol")
  async def _bulk_sol_autocomplete(
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

  @bulk_cmd.autocomplete("tag")
  async def _bulk_tag_autocomplete(
    self,
    interaction: discord.Interaction,
    current: str,
  ) -> list[app_commands.Choice[str]]:
    return [
      app_commands.Choice(name=t, value=t)
      for t in _TAGS
      if current.lower() in t.lower()
    ]

  # ── /run list ─────────────────────────────────────────────────────────────

  @run.command(
    name="list",
    description="List recent MYSTRAN runs, optionally filtered to a deck",
  )
  @app_commands.describe(
    deck_id="Deck ID to filter by (optional — omit to list all recent runs)",
    page="Page number (default: 1)",
  )
  async def list_cmd(
    self,
    interaction: discord.Interaction,
    deck_id: int | None = None,
    page: int = 1,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if deck_id is not None:
        deck = await get_deck(session, deck_id)
        if deck is None:
          await interaction.response.send_message(
            f"No deck with ID `{deck_id}`.", ephemeral=True
          )
          return

      runs, total = await list_runs_for_deck(
        session, deck_id, page=page, per_page=10
      )

    if not runs:
      msg = (
        f"No runs recorded for deck `#{deck_id}` yet."
        if deck_id is not None
        else "No runs recorded yet."
      )
      await interaction.response.send_message(msg, ephemeral=ephemeral)
      return

    total_pages = max(1, math.ceil(total / 10))
    title = (
      f"Runs for deck #{deck_id} — page {page}/{total_pages} (total: {total})"
      if deck_id is not None
      else f"Recent runs — page {page}/{total_pages} (total: {total})"
    )
    embed = discord.Embed(title=title, colour=discord.Colour.orange())
    for r in runs:
      _add_run_field(embed, r)
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

  # ── /run search ──────────────────────────────────────────────────────────

  @run.command(
    name="search",
    description="Search runs with filters",
  )
  @app_commands.describe(
    deck="Filter by deck ID (number) or filename substring",
    status="Filter by run status",
    finish="Filter by finish type (normal/fatal/crash/none)",
    node="Filter by compute node name",
    batch_id="Filter by batch ID",
    submitter="Filter by the Discord member who submitted the run",
    min_elapsed="Minimum elapsed time, e.g. 30s / 5m / 1h",
    max_elapsed="Maximum elapsed time, e.g. 30s / 5m / 1h",
    valgrind="Filter by valgrind result",
    sort_by="Sort order",
  )
  @app_commands.choices(
    status=[
      app_commands.Choice(name="pending", value="pending"),
      app_commands.Choice(name="building", value="building"),
      app_commands.Choice(name="running", value="running"),
      app_commands.Choice(name="completed", value="completed"),
      app_commands.Choice(name="failed", value="failed"),
      app_commands.Choice(name="cancelled", value="cancelled"),
    ],
    finish=[
      app_commands.Choice(name="normal", value="normal"),
      app_commands.Choice(name="fatal", value="fatal"),
      app_commands.Choice(name="crash", value="crash"),
      app_commands.Choice(name="none (not yet set)", value="none"),
    ],
    valgrind=[
      app_commands.Choice(name="clean (0 errors)", value="clean"),
      app_commands.Choice(name="errors found", value="errors"),
      app_commands.Choice(name="no data", value="no_data"),
    ],
    sort_by=[
      app_commands.Choice(name="newest first", value="newest"),
      app_commands.Choice(name="oldest first", value="oldest"),
      app_commands.Choice(name="longest elapsed", value="longest"),
      app_commands.Choice(name="shortest elapsed", value="shortest"),
    ],
  )
  async def search_cmd(
    self,
    interaction: discord.Interaction,
    deck: str | None = None,
    status: str | None = None,
    finish: str | None = None,
    node: str | None = None,
    batch_id: int | None = None,
    submitter: discord.Member | None = None,
    min_elapsed: str | None = None,
    max_elapsed: str | None = None,
    valgrind: str | None = None,
    sort_by: str = "newest",
  ) -> None:
    # Validate elapsed strings up front.
    min_elapsed_s: int | None = None
    max_elapsed_s: int | None = None
    if min_elapsed is not None:
      min_elapsed_s = _parse_duration(min_elapsed)
      if min_elapsed_s is None:
        await interaction.response.send_message(
          f"Couldn't parse `min_elapsed` value `{min_elapsed}`. "
          "Use formats like `30s`, `5m`, `1h`, `1h30m`.",
          ephemeral=True,
        )
        return
    if max_elapsed is not None:
      max_elapsed_s = _parse_duration(max_elapsed)
      if max_elapsed_s is None:
        await interaction.response.send_message(
          f"Couldn't parse `max_elapsed` value `{max_elapsed}`. "
          "Use formats like `30s`, `5m`, `1h`, `1h30m`.",
          ephemeral=True,
        )
        return

    # Resolve deck to ID or name, and node to ID.
    deck_id: int | None = None
    deck_name: str | None = None
    if deck is not None:
      try:
        deck_id = int(deck)
      except ValueError:
        deck_name = deck

    node_id: int | None = None
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)

      if node is not None:
        node_row = await get_node_by_name(session, node)
        if node_row is None:
          await interaction.response.send_message(
            f"No node named `{node}`.", ephemeral=True
          )
          return
        node_id = node_row.id

      params = RunSearchParams(
        deck_id=deck_id,
        deck_name=deck_name,
        status=status,
        finish=finish,
        node=node,
        node_id=node_id,
        batch_id=batch_id,
        submitted_by_id=submitter.id if submitter is not None else None,
        submitted_by_name=(
          submitter.display_name if submitter is not None else None
        ),
        min_elapsed_s=min_elapsed_s,
        max_elapsed_s=max_elapsed_s,
        min_elapsed_label=min_elapsed,
        max_elapsed_label=max_elapsed,
        valgrind=valgrind,
        sort_by=sort_by,
      )

      runs, total = await search_runs(
        session,
        deck_id=params.deck_id,
        deck_name=params.deck_name,
        status=params.status,
        finish=params.finish,
        node_id=params.node_id,
        batch_id=params.batch_id,
        submitted_by=params.submitted_by_id,
        min_elapsed_s=params.min_elapsed_s,
        max_elapsed_s=params.max_elapsed_s,
        valgrind=params.valgrind,
        sort_by=params.sort_by,
        page=1,
      )

    if not runs:
      await interaction.response.send_message(
        "No runs match those filters.", ephemeral=ephemeral
      )
      return

    view = RunSearchView(
      params=params,
      runs=runs,
      total=total,
      page=1,
      ephemeral=ephemeral,
      invoker_id=interaction.user.id,
    )
    await interaction.response.send_message(
      embed=view._build_embed(), view=view, ephemeral=ephemeral
    )
    view.message = await interaction.original_response()

  # ── /run status ───────────────────────────────────────────────────────────

  @run.command(
    name="status",
    description="Show details for a single MYSTRAN run",
  )
  @app_commands.describe(run_id="Run ID")
  async def status_cmd(
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

    api_public_url = get_settings().api_public_url
    embed = _build_run_embed(run, api_public_url)
    done = run.status in _TERMINAL_STATUSES
    view = (
      discord.utils.MISSING if done else RunStatusView(run_id, api_public_url)
    )
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=ephemeral
    )

  # ── /run batches ──────────────────────────────────────────────────────────

  @run.command(
    name="batches",
    description="List recent run batches (5 per page)",
  )
  @app_commands.describe(page="Page number (default: 1)")
  async def batches_cmd(
    self,
    interaction: discord.Interaction,
    page: int = 1,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
      batches, total = await list_recent_batches(session, page=page)

    if not batches:
      await interaction.response.send_message(
        "No batches recorded yet.", ephemeral=ephemeral
      )
      return

    total_pages = max(1, math.ceil(total / 5))
    embed = discord.Embed(
      title=f"Run batches — page {page}/{total_pages} (total: {total})",
      colour=discord.Colour.blurple(),
    )
    api_public_url = get_settings().api_public_url
    for batch in batches:
      ref = batch.version.ref_name or batch.version.commit_hash[:8]
      version_str = f"`{batch.version.repo_name}@{ref}`"
      label_str = f" — {batch.label}" if batch.label else ""
      created_str = _ts(batch.created_at.replace(tzinfo=UTC))
      embed.add_field(
        name=f"Batch #{batch.id}{label_str}",
        value=(f"{version_str} · <@{batch.submitted_by}> · {created_str}"),
        inline=False,
      )
    embed.set_footer(text="Use the buttons below to view a batch's details.")

    view = _BatchListView(batches, api_public_url, ephemeral)
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=ephemeral
    )

  # ── /run batch ────────────────────────────────────────────────────────────

  @run.command(
    name="batch",
    description="Show the summary for a specific run batch",
  )
  @app_commands.describe(batch_id="Batch ID")
  async def batch_cmd(
    self,
    interaction: discord.Interaction,
    batch_id: int,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
      batch = await get_batch(session, batch_id)
      if batch is None:
        await interaction.response.send_message(
          f"No batch with ID `{batch_id}`.", ephemeral=True
        )
        return
      summary = await get_batch_summary(session, batch_id)

    api_public_url = get_settings().api_public_url
    total_run_pages = max(1, math.ceil(summary.total / RUNS_PER_BATCH_PAGE))
    embed = _build_batch_summary_embed(batch, summary)
    view = BatchView(
      batch_id,
      api_public_url,
      ephemeral,
      page=0,
      total_run_pages=total_run_pages,
    )
    await interaction.response.send_message(
      embed=embed, view=view, ephemeral=ephemeral
    )

  # ── Context menu: "Run this deck" ─────────────────────────────────────────

  async def _ctx_run_deck(
    self,
    interaction: discord.Interaction,
    message: discord.Message,
  ) -> None:
    async with get_session() as session:
      deckbot_ch_id = await get_deckbot_channel_id(session)
      ephemeral = _is_ephemeral(interaction, deckbot_ch_id)
      decks = await _resolve_decks_from_message(message, session)

    if not decks:
      await interaction.response.send_message(
        "No processed deck found in that message.", ephemeral=True
      )
      return

    if len(decks) == 1:
      modal = RunDeckModal(ephemeral=ephemeral, preset_deck_id=decks[0].id)
      await interaction.response.send_modal(modal)
      return

    view = _DeckSelectView(decks, ephemeral)
    desc = "\n".join(f"`#{d.id}` {d.filename}" for d in decks)
    await interaction.response.send_message(
      f"That message contains **{len(decks)}** decks. Pick one to run:\n{desc}",
      view=view,
      ephemeral=True,
    )


async def setup(bot: commands.Bot) -> None:
  cog = RunsCog(bot)  # type: ignore[arg-type]
  await bot.add_cog(cog)
  bot.tree.add_command(  # type: ignore[arg-type]
    app_commands.ContextMenu(
      name="Run this deck",
      callback=cog._ctx_run_deck,
      guild_ids=[get_settings().discord_guild_id],
    )
  )
