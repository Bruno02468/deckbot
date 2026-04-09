from __future__ import annotations

import asyncio
import logging

from discord.ext import commands
from sqlalchemy import select

from deckbot.db.models import Job
from deckbot.db.session import get_session
from deckbot.models.job import CrawlChannelPayload, ReprocessChannelPayload
from deckbot.services.crawler import crawl_channel
from deckbot.services.reprocessor import reprocess_channel

log = logging.getLogger(__name__)

# How long to wait (seconds) when the job queue is empty before checking again.
_POLL_IDLE = 30
# Short pause between consecutive jobs when the queue is not empty.
_POLL_BUSY = 2


class JobRunner:
  """Background asyncio task that drains the jobs table one entry at a time."""

  def __init__(self, bot: commands.Bot) -> None:
    self._bot = bot
    self._task: asyncio.Task[None] | None = None

  def start(self) -> None:
    self._task = asyncio.create_task(self._run(), name="job_runner")

  def stop(self) -> None:
    if self._task is not None:
      self._task.cancel()

  async def _run(self) -> None:
    log.info("Job runner started")
    await self._reset_stale_jobs()

    while True:
      job = await self._claim_next_job()
      if job is None:
        await asyncio.sleep(_POLL_IDLE)
        continue

      await self._execute(job)
      await asyncio.sleep(_POLL_BUSY)

  async def _reset_stale_jobs(self) -> None:
    """Flip any 'running' jobs back to 'pending' after an unclean shutdown."""
    async with get_session() as session:
      result = await session.execute(
        select(Job).where(Job.status == "running")
      )
      stale = result.scalars().all()
      for job in stale:
        job.status = "pending"
        job.error = "Reset after restart"
      await session.commit()
    if stale:
      log.info("Reset %d stale running job(s) to pending", len(stale))

  async def _claim_next_job(self) -> Job | None:
    """Atomically mark the oldest pending job as 'running' and return it.

    Returns None when the queue is empty.
    The returned Job is detached from its session (expire_on_commit=False
    keeps all attributes accessible after the session closes).
    """
    async with get_session() as session:
      result = await session.execute(
        select(Job)
        .where(Job.status == "pending")
        .order_by(Job.created_at)
        .limit(1)
      )
      job = result.scalar_one_or_none()
      if job is None:
        return None

      job.status = "running"
      await session.commit()
      # Detach so the object survives beyond the session context.
      session.expunge(job)
      return job

  async def _execute(self, job: Job) -> None:
    log.info("Starting job #%d: %s", job.id, job.type)
    try:
      if job.type == "crawl_channel":
        payload = CrawlChannelPayload.model_validate_json(job.payload or "{}")
        async with get_session() as session:
          await crawl_channel(payload.channel_id, self._bot, session)
      elif job.type == "reprocess_channel":
        payload = ReprocessChannelPayload.model_validate_json(
          job.payload or "{}"
        )
        async with get_session() as session:
          await reprocess_channel(payload.channel_id, session)
      else:
        raise ValueError(f"Unknown job type: {job.type!r}")

    except Exception as exc:
      log.exception("Job #%d failed", job.id)
      await self._update_job(job.id, "failed", str(exc))
    else:
      log.info("Job #%d completed", job.id)
      await self._update_job(job.id, "completed")

  async def _update_job(
    self, job_id: int, status: str, error: str | None = None
  ) -> None:
    async with get_session() as session:
      job = await session.get(Job, job_id)
      if job is not None:
        job.status = status
        job.error = error
        await session.commit()
