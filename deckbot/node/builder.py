from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from deckbot.node.config import NodeSettings

log = logging.getLogger(__name__)

# One asyncio.Lock per repo name so that only one build at a time runs against
# the shared clone directory for that repo.  Created lazily within async
# context to avoid event-loop issues.
_build_locks: dict[str, asyncio.Lock] = {}


def _lock_for(repo_name: str) -> asyncio.Lock:
  if repo_name not in _build_locks:
    _build_locks[repo_name] = asyncio.Lock()
  return _build_locks[repo_name]


async def _run(args: list[str], cwd: Path) -> tuple[int, str, str]:
  """Run *args* as a subprocess under *cwd*; return (returncode, stdout, stderr)."""
  proc = await asyncio.create_subprocess_exec(
    *args,
    cwd=cwd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
  )
  stdout, stderr = await proc.communicate()
  return (
    proc.returncode or 0,
    stdout.decode(errors="replace"),
    stderr.decode(errors="replace"),
  )


async def get_binary(
  repo_name: str,
  repo_url: str,
  commit_hash: str,
  config: NodeSettings,
) -> Path:
  """Return the path to a built MYSTRAN binary for *commit_hash*.

  Checks the binary cache first.  If not present, acquires a per-repo lock
  and builds, then caches the result.

  Cache layout::

      {build_cache_dir}/
        binaries/{repo_name}/{commit_hash}/mystran   ← cached binary
        repos/{repo_name}/                           ← shared git clone
  """
  binary_path = (
    config.build_cache_dir / "binaries" / repo_name / commit_hash / "mystran"
  )
  if binary_path.exists():
    log.debug("Cache hit: %s", binary_path)
    return binary_path

  async with _lock_for(repo_name):
    # Re-check after acquiring lock (another task may have built it).
    if binary_path.exists():
      return binary_path
    await _build(repo_name, repo_url, commit_hash, binary_path, config)
    return binary_path


async def _build(
  repo_name: str,
  repo_url: str,
  commit_hash: str,
  binary_dest: Path,
  config: NodeSettings,
) -> None:
  """Clone / fetch, check out *commit_hash*, build, and cache the binary."""
  repo_dir = config.build_cache_dir / "repos" / repo_name

  # ── Clone or fetch ──────────────────────────────────────────────────────
  if (repo_dir / ".git").exists():
    log.info("[%s] Fetching latest from %s ...", repo_name, repo_url)
    rc, _, err = await _run(
      ["git", "fetch", "--all", "--tags", "--prune"], repo_dir
    )
    if rc != 0:
      raise RuntimeError(f"git fetch failed: {err.strip()}")
  else:
    log.info("[%s] Cloning %s ...", repo_name, repo_url)
    repo_dir.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _run(
      ["git", "clone", repo_url, str(repo_dir)], repo_dir.parent
    )
    if rc != 0:
      raise RuntimeError(f"git clone failed: {err.strip()}")

  # ── Clean working tree ──────────────────────────────────────────────────
  log.info("[%s] Cleaning repo ...", repo_name)
  for cmd in [
    # Submodules first (matches mystran_picker order), then main repo.
    # Cleaning submodules before the main repo avoids losing their
    # directories before git-submodule-foreach can reach them.
    [
      "git",
      "submodule",
      "foreach",
      "--recursive",
      "git",
      "clean",
      "-fdx",
    ],
    [
      "git",
      "submodule",
      "foreach",
      "--recursive",
      "git",
      "reset",
      "--hard",
    ],
    ["git", "clean", "-fdx"],
    ["git", "reset", "--hard"],
  ]:
    await _run(cmd, repo_dir)  # best-effort; ignore failures

  # ── Checkout ────────────────────────────────────────────────────────────
  log.info("[%s] Checking out %s ...", repo_name, commit_hash)
  rc, _, err = await _run(["git", "checkout", commit_hash], repo_dir)
  if rc != 0:
    raise RuntimeError(f"git checkout {commit_hash} failed: {err.strip()}")

  # ── Submodules ──────────────────────────────────────────────────────────
  log.info("[%s] Updating submodules ...", repo_name)
  rc, _, err = await _run(
    ["git", "submodule", "update", "--init", "--recursive"], repo_dir
  )
  if rc != 0:
    raise RuntimeError(f"git submodule update failed: {err.strip()}")

  # ── CMake configure ─────────────────────────────────────────────────────
  log.info("[%s] Configuring (cmake) ...", repo_name)
  rc, _, err = await _run(
    [
      "cmake",
      "-G",
      "Unix Makefiles",
      "-DCMAKE_BUILD_TYPE=Debug",
      "-Denable_internal_blaslib=yes",
      ".",
    ],
    repo_dir,
  )
  if rc != 0:
    raise RuntimeError(f"cmake configure failed: {err.strip()}")

  # ── CMake build ─────────────────────────────────────────────────────────
  jobs = str(config.max_threads)
  log.info("[%s] Building with -j%s ...", repo_name, jobs)
  rc, _, err = await _run(["cmake", "--build", ".", f"-j{jobs}"], repo_dir)
  if rc != 0:
    raise RuntimeError(f"cmake build failed: {err.strip()}")

  # ── Copy binary to cache ─────────────────────────────────────────────────
  src = repo_dir / "Binaries" / "mystran"
  if not src.exists():
    raise RuntimeError("Build succeeded but Binaries/mystran was not found")

  binary_dest.parent.mkdir(parents=True, exist_ok=True)
  shutil.copy2(src, binary_dest)
  binary_dest.chmod(0o755)
  log.info("[%s] Binary cached at %s", repo_name, binary_dest)
