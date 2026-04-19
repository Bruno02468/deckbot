from __future__ import annotations

import io
import zipfile
from typing import Annotated

import zstandard as zstd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from deckbot.api.deps import get_db_session
from deckbot.db.models import Run, RunFile

router = APIRouter(tags=["files"])

_DECOMPRESSOR = zstd.ZstdDecompressor()

# Content-types served inline (browser opens as text) rather than as download.
_INLINE_SUFFIXES = {".f06", ".f04", ".log", ".txt"}


def _get_content_type(filename: str) -> tuple[str, str]:
  """Return (content_type, disposition) for a run file."""
  lower = filename.lower()
  for suffix in _INLINE_SUFFIXES:
    if lower.endswith(suffix):
      return "text/plain; charset=utf-8", "inline"
  return "application/octet-stream", "attachment"


async def _load_run_with_files(
  session: AsyncSession, run_id: int
) -> Run | None:
  result = await session.execute(
    select(Run).options(selectinload(Run.files)).where(Run.id == run_id)
  )
  return result.scalar_one_or_none()


@router.get("/run/{run_id}/files/{filename}")
async def get_run_file(
  run_id: int,
  filename: str,
  session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
  """Serve a single output file from a completed run.

  ``.f06`` and other text files are served inline (``text/plain``) so they
  open directly in a browser.  All other files are served as downloads.
  """
  run = await _load_run_with_files(session, run_id)
  if run is None:
    raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

  match = next((f for f in run.files if f.filename == filename), None)
  if match is None:
    raise HTTPException(
      status_code=404,
      detail=f"File '{filename}' not found in run {run_id}",
    )

  raw = _DECOMPRESSOR.decompress(match.content)
  content_type, disposition = _get_content_type(filename)
  headers = {
    "Content-Disposition": f'{disposition}; filename="{filename}"',
    "Content-Length": str(len(raw)),
  }
  return Response(content=raw, media_type=content_type, headers=headers)


@router.get("/run/{run_id}/zip")
async def get_run_zip(
  run_id: int,
  session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
  """Download all output files for a run as a zip archive."""
  run = await _load_run_with_files(session, run_id)
  if run is None:
    raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

  if not run.files:
    raise HTTPException(
      status_code=404,
      detail=f"Run {run_id} has no output files",
    )

  buf = io.BytesIO()
  with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
    for rf in run.files:
      raw = _DECOMPRESSOR.decompress(rf.content)
      zf.writestr(rf.filename, raw)
  buf.seek(0)

  zip_name = f"run_{run_id}.zip"
  headers = {
    "Content-Disposition": f'attachment; filename="{zip_name}"',
  }
  return StreamingResponse(
    buf,
    media_type="application/zip",
    headers=headers,
  )
