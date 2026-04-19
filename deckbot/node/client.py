from __future__ import annotations

import asyncio
import logging

import httpx

from deckbot.models.run import JobItem
from deckbot.node.config import NodeSettings
from deckbot.node.runner import run_job

log = logging.getLogger(__name__)


class NodeClient:
  """Main node event loop: keepalive + job polling.

  Sends a keepalive to the API on startup and then every
  ``config.keepalive_interval`` seconds.  Polls for new jobs whenever
  there are free slots (up to ``config.max_threads`` concurrent runs).
  """

  def __init__(self, config: NodeSettings) -> None:
    self._config = config
    self._http = httpx.AsyncClient(
      base_url=config.api_endpoint,
      headers={"X-API-Key": config.api_key},
      # Generous timeout — job-completion uploads can be large.
      timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
    )
    # Number of currently running jobs.  Safe without a lock since asyncio
    # is single-threaded; incremented/decremented only in async callbacks.
    self._active: int = 0

  async def run(self) -> None:
    """Start and run until cancelled (e.g. KeyboardInterrupt / SIGTERM)."""
    config = self._config
    config.build_cache_dir.mkdir(parents=True, exist_ok=True)
    config.work_base_dir.mkdir(parents=True, exist_ok=True)

    log.info(
      "Node starting — endpoint=%s max_threads=%d",
      config.api_endpoint,
      config.max_threads,
    )

    # Send an immediate keepalive before entering the loops.
    await self._keepalive()

    async with asyncio.TaskGroup() as tg:
      tg.create_task(self._keepalive_loop())
      tg.create_task(self._poll_loop())

  # ── Keepalive ────────────────────────────────────────────────────────────

  async def _keepalive(self) -> None:
    try:
      resp = await self._http.post(
        "/api/v1/keepalive",
        json={"max_threads": self._config.max_threads},
      )
      resp.raise_for_status()
      log.debug("Keepalive OK")
    except Exception as exc:
      log.warning("Keepalive failed: %s", exc)

  async def _keepalive_loop(self) -> None:
    while True:
      await asyncio.sleep(self._config.keepalive_interval)
      await self._keepalive()

  # ── Job polling ──────────────────────────────────────────────────────────

  async def _poll_loop(self) -> None:
    while True:
      available = self._config.max_threads - self._active
      if available > 0:
        try:
          jobs = await self._fetch_jobs(available)
        except Exception as exc:
          log.warning("Job poll failed: %s", exc)
          await asyncio.sleep(self._config.poll_interval)
          continue

        if jobs:
          log.info("Claimed %d job(s)", len(jobs))
          for job in jobs:
            asyncio.ensure_future(self._run_job(job))
          # Poll again quickly in case there are more jobs waiting.
          await asyncio.sleep(2.0)
          continue

      # Nothing to do — wait the full poll interval.
      await asyncio.sleep(self._config.poll_interval)

  async def _fetch_jobs(self, slots: int) -> list[JobItem]:
    resp = await self._http.get("/api/v1/jobs/next", params={"slots": slots})
    resp.raise_for_status()
    return [JobItem.model_validate(item) for item in resp.json()]

  async def _run_job(self, job: JobItem) -> None:
    self._active += 1
    try:
      await run_job(job, self._http, self._config)
    finally:
      self._active -= 1
