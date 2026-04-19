from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Annotated

import zstandard as zstd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.api.auth import require_active_node
from deckbot.api.deps import get_db_session
from deckbot.db.models import Node, Run, RunFile
from deckbot.db.queries import (
  claim_pending_runs,
  get_run,
  get_setting,
)
from deckbot.models.repo import APPROVED_REPOS
from deckbot.models.run import CompleteMetadata, JobItem

router = APIRouter(tags=["jobs"])

_DECOMPRESSOR = zstd.ZstdDecompressor()
_COMPRESSOR = zstd.ZstdCompressor(level=9)

# Default maximum total uncompressed bytes accepted per /complete upload.
_DEFAULT_ARTIFACT_LIMIT = 100 * 1024 * 1024  # 100 MB


class FailRequest(BaseModel):
  error: str


async def _get_artifact_limit(session: AsyncSession) -> int:
  raw = await get_setting(session, "run_artifact_limit_bytes")
  if raw is not None:
    try:
      return int(raw)
    except ValueError:
      pass
  return _DEFAULT_ARTIFACT_LIMIT


def _get_run_or_404(run: Run | None, run_id: int) -> Run:
  if run is None:
    raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
  return run


def _assert_run_owned_by_node(run: Run, node: Node) -> None:
  if run.node_id != node.id:
    raise HTTPException(
      status_code=403, detail="This run was not assigned to your node"
    )


@router.get("/jobs/next", response_model=list[JobItem])
async def get_next_jobs(
  slots: int = 1,
  node: Node = Depends(require_active_node),
  session: AsyncSession = Depends(get_db_session),
) -> list[JobItem]:
  """Claim and return up to *slots* pending runs for this node.

  Each item includes the full raw deck content (base64-encoded) and the
  MYSTRAN version details needed to build and execute the run.
  """
  if slots < 1:
    slots = 1
  elif slots > 16:
    slots = 16

  runs = await claim_pending_runs(session, node_id=node.id, slots=slots)
  await session.commit()

  items: list[JobItem] = []
  for run in runs:
    repo_url = APPROVED_REPOS.get(run.version.repo_name)
    if repo_url is None:
      # Repo was removed from the approved list after the run was queued.
      # Mark it failed rather than sending it to the node.
      run.status = "failed"
      run.error = (
        f"Repo '{run.version.repo_name}' is no longer in APPROVED_REPOS"
      )
      run.completed_at = datetime.now(UTC)
      continue

    raw_bytes = _DECOMPRESSOR.decompress(run.deck.content)
    items.append(
      JobItem(
        run_id=run.id,
        deck_id=run.deck_id,
        deck_filename=run.deck.filename,
        deck_content=base64.b64encode(raw_bytes).decode(),
        repo_name=run.version.repo_name,
        commit_hash=run.version.commit_hash,
        repo_url=repo_url,
      )
    )

  if any(r.status == "failed" for r in runs):
    await session.commit()

  return items


@router.post("/jobs/{run_id}/complete", status_code=204)
async def complete_job(
  run_id: int,
  metadata: Annotated[str, Form()],
  files: Annotated[list[UploadFile], File()],
  node: Node = Depends(require_active_node),
  session: AsyncSession = Depends(get_db_session),
) -> None:
  """Mark a run as completed and upload its output files.

  ``metadata`` is a JSON-encoded :class:`CompleteMetadata` object.
  ``files`` is a list of multipart file parts — one per output artifact.
  """
  run = _get_run_or_404(await get_run(session, run_id), run_id)
  _assert_run_owned_by_node(run, node)

  if run.status != "running":
    raise HTTPException(
      status_code=409,
      detail=f"Run {run_id} is not in 'running' state (got '{run.status}')",
    )

  try:
    meta = CompleteMetadata.model_validate(json.loads(metadata))
  except Exception as exc:
    raise HTTPException(
      status_code=422, detail=f"Invalid metadata JSON: {exc}"
    ) from exc

  limit = await _get_artifact_limit(session)
  total_size = 0

  run_files: list[RunFile] = []
  for upload in files:
    raw = await upload.read()
    total_size += len(raw)
    if total_size > limit:
      raise HTTPException(
        status_code=413,
        detail=(f"Total artifact size exceeds limit of {limit} bytes"),
      )
    compressed = _COMPRESSOR.compress(raw)
    run_files.append(
      RunFile(
        run_id=run_id,
        filename=upload.filename or "unknown",
        content=compressed,
        size_bytes=len(raw),
        stored_at=datetime.now(UTC),
      )
    )

  for rf in run_files:
    session.add(rf)

  run.status = "completed"
  run.exit_code = meta.exit_code
  run.finish = meta.finish
  run.valgrind_errors = meta.valgrind_errors
  run.completed_at = datetime.now(UTC)

  await session.commit()


@router.post("/jobs/{run_id}/fail", status_code=204)
async def fail_job(
  run_id: int,
  body: FailRequest,
  node: Node = Depends(require_active_node),
  session: AsyncSession = Depends(get_db_session),
) -> None:
  """Mark a run as failed with an error message."""
  run = _get_run_or_404(await get_run(session, run_id), run_id)
  _assert_run_owned_by_node(run, node)

  if run.status != "running":
    raise HTTPException(
      status_code=409,
      detail=f"Run {run_id} is not in 'running' state (got '{run.status}')",
    )

  run.status = "failed"
  run.error = body.error
  run.completed_at = datetime.now(UTC)
  await session.commit()
