from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from deckbot.models.run import CompleteMetadata, JobItem
from deckbot.node.builder import get_binary
from deckbot.node.config import NodeSettings
from deckbot.node.sandbox import build_command

log = logging.getLogger(__name__)

# Input deck extensions — these are already in the DB; we skip them when
# collecting output files to upload.
_DECK_EXTS = {".bdf", ".dat", ".nas"}


async def run_job(
  job: JobItem,
  client: httpx.AsyncClient,
  config: NodeSettings,
) -> None:
  """Execute one job end-to-end: build → sandbox → collect → upload.

  Creates a temporary work directory, writes the deck there, builds (or
  retrieves) the MYSTRAN binary, runs it under valgrind, then uploads all
  output files to the API.  The work directory is always cleaned up on exit.
  """
  config.work_base_dir.mkdir(parents=True, exist_ok=True)
  work_dir = Path(
    tempfile.mkdtemp(prefix="deckbot_run_", dir=config.work_base_dir)
  )
  log.info("Run #%d starting — work_dir=%s", job.run_id, work_dir)

  try:
    await _execute(job, client, config, work_dir)
  except Exception as exc:
    log.exception("Run #%d failed: %s", job.run_id, exc)
    try:
      resp = await client.post(
        f"/api/v1/jobs/{job.run_id}/fail",
        json={"error": str(exc)},
      )
      resp.raise_for_status()
    except Exception:
      log.exception("Also failed to report failure for run #%d", job.run_id)
  finally:
    shutil.rmtree(work_dir, ignore_errors=True)
    log.info("Run #%d work dir cleaned up", job.run_id)


async def _execute(
  job: JobItem,
  client: httpx.AsyncClient,
  config: NodeSettings,
  work_dir: Path,
) -> None:
  # ── Write deck to work dir ──────────────────────────────────────────────
  # Keep the original filename so MYSTRAN names its output files accordingly
  # (e.g. foo.DAT → foo.F06 rather than deck.F06).
  deck_path = work_dir / job.deck_filename
  deck_path.write_bytes(base64.b64decode(job.deck_content))

  # ── Build / retrieve MYSTRAN binary ────────────────────────────────────
  log.info(
    "Run #%d: resolving binary %s@%s",
    job.run_id,
    job.repo_name,
    job.commit_hash[:8],
  )
  binary = await get_binary(
    job.repo_name, job.repo_url, job.commit_hash, config
  )

  # ── Run under valgrind ──────────────────────────────────────────────────
  valgrind_xml = work_dir / "valgrind.xml"
  cmd = build_command(binary, deck_path, valgrind_xml)

  log.info("Run #%d: executing (cwd=%s)", job.run_id, work_dir)
  import asyncio

  proc = await asyncio.create_subprocess_exec(
    *cmd,
    cwd=work_dir,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
  )
  stdout_bytes, stderr_bytes = await proc.communicate()
  exit_code = proc.returncode or 0

  (work_dir / "stdout.txt").write_bytes(stdout_bytes)
  (work_dir / "stderr.txt").write_bytes(stderr_bytes)
  log.info("Run #%d: exit_code=%d", job.run_id, exit_code)

  # ── Parse valgrind XML ──────────────────────────────────────────────────
  valgrind_errors: int | None = None
  if valgrind_xml.exists():
    xml_text = valgrind_xml.read_text(errors="replace")
    # Count individual <error> elements reported by memcheck.
    valgrind_errors = len(re.findall(r"<error>", xml_text))

  # ── Determine finish classification ────────────────────────────────────
  # Check the F06 for a controlled MYSTRAN fatal before looking at exit code.
  # If no F06 was produced, fall back to stdout: "FATAL" in stdout means a
  # fatal that occurred before the F06 was written; otherwise it's a crash.
  f06_files = list(work_dir.glob("*.f06")) + list(work_dir.glob("*.F06"))
  finish: str
  if f06_files and "FATAL MESSAGE" in f06_files[0].read_text(errors="replace"):
    finish = "fatal"
  elif not f06_files and "FATAL" in stdout_bytes.decode(errors="replace"):
    finish = "fatal"
  elif exit_code != 0:
    finish = "crash"
  else:
    finish = "normal"

  # ── Collect output files ────────────────────────────────────────────────
  # Upload everything in the work dir except the original input deck.
  output_files: list[Path] = [
    f
    for f in work_dir.iterdir()
    if f.is_file() and f.suffix.lower() not in _DECK_EXTS
  ]

  # ── Upload results ──────────────────────────────────────────────────────
  meta = CompleteMetadata(
    exit_code=exit_code,
    finish=finish,
    valgrind_errors=valgrind_errors,
  )
  file_parts: list[tuple[str, tuple[str, bytes, str]]] = [
    ("files", (f.name, f.read_bytes(), "application/octet-stream"))
    for f in output_files
  ]

  resp = await client.post(
    f"/api/v1/jobs/{job.run_id}/complete",
    data={"metadata": meta.model_dump_json()},
    files=file_parts if file_parts else None,
  )
  resp.raise_for_status()
  log.info(
    "Run #%d: complete — %d output file(s) uploaded",
    job.run_id,
    len(file_parts),
  )
