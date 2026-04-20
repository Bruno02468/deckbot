from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.db.models import (
  Channel,
  Deck,
  DeckTag,
  Job,
  MystranVersion,
  Node,
  Run,
  RunBatch,
  RunFile,
  Setting,
)
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


async def get_decks_by_message(
  session: AsyncSession, message_id: int
) -> list[Deck]:
  """Return all decks whose source message is *message_id*."""
  result = await session.execute(
    select(Deck)
    .options(selectinload(Deck.tags))
    .where(Deck.source_message_id == message_id)
    .order_by(Deck.id)
  )
  return list(result.scalars().all())


async def get_decks_by_hashes(
  session: AsyncSession, hashes: list[str]
) -> list[Deck]:
  """Return decks matching any of the given SHA-256 hashes, ordered by id."""
  result = await session.execute(
    select(Deck)
    .options(selectinload(Deck.tags))
    .where(Deck.hash.in_(hashes))
    .order_by(Deck.id)
  )
  return list(result.scalars().all())


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


# ── Nodes ─────────────────────────────────────────────────────────────────────


async def get_node_by_key_hash(
  session: AsyncSession, key_hash: str
) -> Node | None:
  result = await session.execute(
    select(Node).where(
      Node.api_key_hash == key_hash,
      Node.is_active == True,  # noqa: E712
    )
  )
  return result.scalar_one_or_none()


async def get_node_by_name(session: AsyncSession, name: str) -> Node | None:
  result = await session.execute(select(Node).where(Node.name == name))
  return result.scalar_one_or_none()


async def list_nodes(session: AsyncSession) -> list[Node]:
  result = await session.execute(select(Node).order_by(Node.created_at.asc()))
  return list(result.scalars().all())


_DEFAULT_ORPHAN_GRACE = 180  # seconds


async def reset_orphaned_runs(
  session: AsyncSession,
  node_id: int,
  reported_run_ids: list[int],
  grace_seconds: int = _DEFAULT_ORPHAN_GRACE,
) -> list[int]:
  """Reset ghost runs back to ``pending`` for re-execution.

  A ghost run is one that the server believes is active on *node_id*
  (``status`` is ``building`` or ``running``) but whose ID was **not**
  included in the node's keepalive report *and* whose ``started_at``
  timestamp is older than *grace_seconds*.  The grace period avoids
  false positives for runs that were just claimed and haven't yet
  appeared in the node's tracking set.

  Resets each orphan to ``pending`` and clears ``node_id``,
  ``started_at``, and ``run_started_at`` so it can be claimed afresh.
  Any stray ``RunFile`` rows (defensive) are deleted first.

  Returns the list of run IDs that were reset.
  """
  cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)

  result = await session.execute(
    select(Run).where(
      Run.node_id == node_id,
      Run.status.in_(("building", "running")),
      Run.started_at < cutoff,
      Run.id.not_in(reported_run_ids) if reported_run_ids else True,
    )
  )
  orphans = list(result.scalars().all())
  if not orphans:
    return []

  orphan_ids = [r.id for r in orphans]

  # Delete any stray output files (shouldn't exist for incomplete runs,
  # but clean up defensively before resetting status).
  await session.execute(delete(RunFile).where(RunFile.run_id.in_(orphan_ids)))

  for run in orphans:
    run.status = "pending"
    run.node_id = None
    run.started_at = None
    run.run_started_at = None
    run.exit_code = None
    run.finish = None
    run.valgrind_errors = None
    run.error = None

  return orphan_ids


# ── MystranVersions ───────────────────────────────────────────────────────────


async def get_or_create_version(
  session: AsyncSession,
  repo_name: str,
  commit_hash: str,
  ref_name: str | None,
) -> MystranVersion:
  """Find an existing (repo_name, commit_hash) pair or create a new one."""
  result = await session.execute(
    select(MystranVersion).where(
      MystranVersion.repo_name == repo_name,
      MystranVersion.commit_hash == commit_hash,
    )
  )
  version = result.scalar_one_or_none()
  if version is None:
    version = MystranVersion(
      repo_name=repo_name,
      commit_hash=commit_hash,
      ref_name=ref_name,
      resolved_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
  return version


# ── Runs ──────────────────────────────────────────────────────────────────────


async def get_run(session: AsyncSession, run_id: int) -> Run | None:
  result = await session.execute(
    select(Run)
    .options(
      selectinload(Run.deck),
      selectinload(Run.version),
      selectinload(Run.node),
      selectinload(Run.files),
    )
    .where(Run.id == run_id)
  )
  return result.scalar_one_or_none()


async def get_active_run_for_deck_version(
  session: AsyncSession, deck_id: int, version_id: int
) -> Run | None:
  """Return an existing pending, building, or running run for (deck, version)."""
  result = await session.execute(
    select(Run).where(
      Run.deck_id == deck_id,
      Run.version_id == version_id,
      Run.status.in_(["pending", "building", "running"]),
    )
  )
  return result.scalar_one_or_none()


async def get_any_run_for_deck_version(
  session: AsyncSession, deck_id: int, version_id: int
) -> Run | None:
  """Return the most recent run for (deck, version), regardless of status."""
  result = await session.execute(
    select(Run)
    .options(
      selectinload(Run.deck),
      selectinload(Run.version),
      selectinload(Run.node),
      selectinload(Run.files),
    )
    .where(Run.deck_id == deck_id, Run.version_id == version_id)
    .order_by(Run.created_at.desc())
    .limit(1)
  )
  return result.scalar_one_or_none()


async def list_runs_for_deck(
  session: AsyncSession,
  deck_id: int | None,
  *,
  page: int = 1,
  per_page: int = 10,
) -> tuple[list[Run], int]:
  """Return (page_of_runs, total_count) newest-first.

  If *deck_id* is ``None``, returns runs across all decks.
  """
  base_q = select(Run).options(
    selectinload(Run.deck),
    selectinload(Run.version),
    selectinload(Run.node),
  )
  count_q = select(func.count()).select_from(Run)
  if deck_id is not None:
    base_q = base_q.where(Run.deck_id == deck_id)
    count_q = count_q.where(Run.deck_id == deck_id)

  total: int = (await session.execute(count_q)).scalar_one()
  result = await session.execute(
    base_q.order_by(Run.created_at.desc())
    .offset((page - 1) * per_page)
    .limit(per_page)
  )
  return list(result.scalars().all()), total


async def claim_pending_runs(
  session: AsyncSession, node_id: int, slots: int
) -> list[Run]:
  """Atomically claim up to `slots` pending runs for a node.

  Marks claimed runs as ``building``, sets ``node_id`` and ``started_at``.
  Returns the list of claimed Run objects (with deck + version loaded).
  """
  result = await session.execute(
    select(Run)
    .options(selectinload(Run.deck), selectinload(Run.version))
    .where(Run.status == "pending")
    .order_by(Run.created_at.asc())
    .limit(slots)
  )
  runs = list(result.scalars().all())
  now = datetime.now(UTC)
  for run in runs:
    run.status = "building"
    run.node_id = node_id
    run.started_at = now
  return runs


async def count_pending_runs_for_deck_version(
  session: AsyncSession,
  *,
  name: str | None = None,
  sol: str | None = None,
  min_grids: int | None = None,
  max_grids: int | None = None,
  tag: str | None = None,
  channel_id: int | None = None,
  version_id: int,
) -> int:
  """Count how many decks matching the filters already have a pending/running
  run for the given version. Used for bulk-run pre-flight checks."""
  subq = select(Deck.id)
  if name is not None:
    subq = subq.where(Deck.filename.ilike(f"%{name}%"))
  if sol is not None:
    subq = subq.where(Deck.sol == sol)
  if min_grids is not None:
    subq = subq.where(Deck.grid_count >= min_grids)
  if max_grids is not None:
    subq = subq.where(Deck.grid_count <= max_grids)
  if tag is not None:
    subq = subq.where(
      Deck.id.in_(select(DeckTag.deck_id).where(DeckTag.tag == tag))
    )
  if channel_id is not None:
    subq = subq.where(Deck.source_channel_id == channel_id)

  count_q = (
    select(func.count())
    .select_from(Run)
    .where(
      Run.version_id == version_id,
      Run.status.in_(["pending", "building", "running"]),
      Run.deck_id.in_(subq),
    )
  )
  return (await session.execute(count_q)).scalar_one()


# ── Batches ───────────────────────────────────────────────────────────────────


@dataclass
class BatchSummary:
  total: int
  by_status: dict[str, int] = field(default_factory=dict)
  # finish breakdown for completed runs only
  by_finish: dict[str, int] = field(default_factory=dict)
  # valgrind breakdown for completed runs only
  valgrind_clean: int = 0
  valgrind_errors_found: int = 0
  valgrind_no_data: int = 0
  # runs where the error column is non-null (infrastructure / node errors)
  infra_errors: int = 0


async def create_batch(
  session: AsyncSession,
  version_id: int,
  submitted_by: int,
  label: str | None,
  filter_summary: str | None,
) -> RunBatch:
  """Persist a new RunBatch and flush so its id is available."""
  batch = RunBatch(
    version_id=version_id,
    submitted_by=submitted_by,
    label=label,
    filter_summary=filter_summary,
    created_at=datetime.now(UTC),
  )
  session.add(batch)
  await session.flush()
  return batch


async def get_batch(session: AsyncSession, batch_id: int) -> RunBatch | None:
  """Return a RunBatch with its version loaded, or None."""
  result = await session.execute(
    select(RunBatch)
    .options(selectinload(RunBatch.version))
    .where(RunBatch.id == batch_id)
  )
  return result.scalar_one_or_none()


async def list_recent_batches(
  session: AsyncSession,
  page: int,
  per_page: int = 5,
) -> tuple[list[RunBatch], int]:
  """Return (page_of_batches, total_count) newest-first, version loaded."""
  count_q = select(func.count()).select_from(RunBatch)
  total: int = (await session.execute(count_q)).scalar_one()

  result = await session.execute(
    select(RunBatch)
    .options(selectinload(RunBatch.version))
    .order_by(RunBatch.created_at.desc())
    .offset((page - 1) * per_page)
    .limit(per_page)
  )
  return list(result.scalars().all()), total


async def get_batch_summary(
  session: AsyncSession, batch_id: int
) -> BatchSummary:
  """Return aggregate statistics for a batch's runs."""
  # Status counts in a single GROUP BY query.
  status_rows = await session.execute(
    select(Run.status, func.count())
    .where(Run.batch_id == batch_id)
    .group_by(Run.status)
  )
  by_status = {row[0]: row[1] for row in status_rows}
  total = sum(by_status.values())

  # Finish breakdown — completed runs only.
  finish_rows = await session.execute(
    select(Run.finish, func.count())
    .where(Run.batch_id == batch_id, Run.status == "completed")
    .group_by(Run.finish)
  )
  by_finish: dict[str, int] = {}
  for finish_val, cnt in finish_rows:
    key = finish_val if finish_val is not None else "unknown"
    by_finish[key] = cnt

  # Valgrind stats — completed runs only.
  valgrind_clean: int = (
    await session.execute(
      select(func.count()).where(
        Run.batch_id == batch_id,
        Run.status == "completed",
        Run.valgrind_errors == 0,
      )
    )
  ).scalar_one()

  valgrind_errors_found: int = (
    await session.execute(
      select(func.count()).where(
        Run.batch_id == batch_id,
        Run.status == "completed",
        Run.valgrind_errors > 0,
      )
    )
  ).scalar_one()

  valgrind_no_data: int = (
    await session.execute(
      select(func.count()).where(
        Run.batch_id == batch_id,
        Run.status == "completed",
        Run.valgrind_errors.is_(None),
      )
    )
  ).scalar_one()

  # Infrastructure errors: any run whose error column is non-null.
  infra_errors: int = (
    await session.execute(
      select(func.count()).where(
        Run.batch_id == batch_id,
        Run.error.is_not(None),
      )
    )
  ).scalar_one()

  return BatchSummary(
    total=total,
    by_status=by_status,
    by_finish=by_finish,
    valgrind_clean=valgrind_clean,
    valgrind_errors_found=valgrind_errors_found,
    valgrind_no_data=valgrind_no_data,
    infra_errors=infra_errors,
  )


async def list_runs_for_batch(
  session: AsyncSession,
  batch_id: int,
  page: int,
  per_page: int = 10,
) -> tuple[list[Run], int]:
  """Return (page_of_runs, total_count) for a batch, newest-first."""
  count_q = (
    select(func.count()).select_from(Run).where(Run.batch_id == batch_id)
  )
  total: int = (await session.execute(count_q)).scalar_one()

  result = await session.execute(
    select(Run)
    .options(
      selectinload(Run.deck),
      selectinload(Run.version),
      selectinload(Run.node),
    )
    .where(Run.batch_id == batch_id)
    .order_by(Run.created_at.asc())
    .offset((page - 1) * per_page)
    .limit(per_page)
  )
  return list(result.scalars().all()), total


async def cancel_batch_runs(session: AsyncSession, batch_id: int) -> int:
  """Cancel all pending and building runs in a batch.

  Returns the number of runs that were cancelled.
  """
  result = await session.execute(
    select(Run).where(
      Run.batch_id == batch_id,
      Run.status.in_(["pending", "building"]),
    )
  )
  runs = list(result.scalars().all())
  now = datetime.now(UTC)
  for run in runs:
    run.status = "cancelled"
    run.completed_at = now
  return len(runs)


# ── Run search ────────────────────────────────────────────────────────────────

RUNS_SEARCH_PER_PAGE = 10


async def search_runs(
  session: AsyncSession,
  *,
  deck_id: int | None = None,
  deck_name: str | None = None,
  status: str | None = None,
  finish: str | None = None,
  node_id: int | None = None,
  batch_id: int | None = None,
  submitted_by: int | None = None,
  min_elapsed_s: int | None = None,
  max_elapsed_s: int | None = None,
  valgrind: str | None = None,
  sort_by: str = "newest",
  page: int = 1,
  per_page: int = RUNS_SEARCH_PER_PAGE,
) -> tuple[list[Run], int]:
  """Filtered, paginated run search.

  *deck_name* does a case-insensitive substring match on Deck.filename.
  *valgrind* accepts: ``"clean"`` (errors == 0), ``"errors"`` (errors > 0),
  ``"no_data"`` (errors IS NULL), or ``None`` (no filter).

  *sort_by* accepts: ``"newest"`` (created_at DESC), ``"oldest"``
  (created_at ASC), ``"longest"`` (elapsed DESC, nulls last),
  ``"shortest"`` (elapsed ASC, nulls last).

  Elapsed is computed as ``completed_at - run_started_at`` when both are
  present, falling back to ``completed_at - started_at``.
  """
  from sqlalchemy import case, nulls_last  # noqa: PLC0415

  base_q = select(Run).options(
    selectinload(Run.deck),
    selectinload(Run.version),
    selectinload(Run.node),
  )
  count_q = select(func.count()).select_from(Run)

  filters = []
  if deck_id is not None:
    filters.append(Run.deck_id == deck_id)
  if deck_name is not None:
    filters.append(
      Run.deck_id.in_(
        select(Deck.id).where(Deck.filename.ilike(f"%{deck_name}%"))
      )
    )
  if status is not None:
    filters.append(Run.status == status)
  if finish is not None:
    if finish == "none":
      filters.append(Run.finish.is_(None))
    else:
      filters.append(Run.finish == finish)
  if node_id is not None:
    filters.append(Run.node_id == node_id)
  if batch_id is not None:
    filters.append(Run.batch_id == batch_id)
  if submitted_by is not None:
    filters.append(Run.submitted_by == submitted_by)
  if valgrind == "clean":
    filters.append(Run.valgrind_errors == 0)
  elif valgrind == "errors":
    filters.append(Run.valgrind_errors > 0)
  elif valgrind == "no_data":
    filters.append(Run.valgrind_errors.is_(None))

  # Elapsed filter: prefer run_started_at if available, fall back to
  # started_at. Only applied when a bound is requested.
  elapsed_expr = case(
    (Run.run_started_at.is_not(None), Run.completed_at - Run.run_started_at),
    else_=Run.completed_at - Run.started_at,
  )
  if min_elapsed_s is not None:
    filters.append(elapsed_expr >= min_elapsed_s)
  if max_elapsed_s is not None:
    filters.append(elapsed_expr <= max_elapsed_s)

  if filters:
    base_q = base_q.where(*filters)
    count_q = count_q.where(*filters)

  # Sorting
  if sort_by == "oldest":
    order = Run.created_at.asc()
  elif sort_by == "longest":
    order = nulls_last(elapsed_expr.desc())
  elif sort_by == "shortest":
    order = nulls_last(elapsed_expr.asc())
  else:  # "newest" (default)
    order = Run.created_at.desc()

  total: int = (await session.execute(count_q)).scalar_one()
  result = await session.execute(
    base_q.order_by(order).offset((page - 1) * per_page).limit(per_page)
  )
  return list(result.scalars().all()), total
