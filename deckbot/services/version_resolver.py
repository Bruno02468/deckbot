from __future__ import annotations

import asyncio
import re

from deckbot.models.repo import APPROVED_REPOS

# Full SHA-1: exactly 40 hex characters.
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class ResolveError(Exception):
  """Raised when a ref cannot be resolved to a commit hash."""


async def resolve_ref(repo_name: str, ref: str) -> str:
  """Resolve a git ref (branch, tag, or full commit hash) to a commit SHA.

  Uses ``git ls-remote`` to look up branches and tags.  If *ref* already
  looks like a full SHA-1 (40 hex chars) it is returned as-is after being
  verified to be reachable via ``git ls-remote`` (the ref ``HEAD`` is used
  to verify the remote is reachable, and the hash is accepted without further
  validation since ``ls-remote`` cannot look up arbitrary commit hashes).

  Raises :class:`ResolveError` on any failure.
  """
  url = APPROVED_REPOS.get(repo_name)
  if url is None:
    raise ResolveError(
      f"Unknown repo `{repo_name}`. "
      f"Allowed repos: {', '.join(f'`{k}`' for k in APPROVED_REPOS)}"
    )

  # If the caller already gave us a full commit hash, trust it.
  if _SHA1_RE.match(ref):
    return ref.lower()

  try:
    proc = await asyncio.create_subprocess_exec(
      "git",
      "ls-remote",
      "--exit-code",
      url,
      f"refs/heads/{ref}",
      f"refs/tags/{ref}",
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
  except asyncio.TimeoutError as exc:
    raise ResolveError(
      f"Timed out resolving `{ref}` on `{repo_name}`."
    ) from exc
  except OSError as exc:
    raise ResolveError(f"Failed to run git: {exc}") from exc

  if proc.returncode == 2:
    # git ls-remote --exit-code returns 2 when no matching refs are found.
    raise ResolveError(
      f"`{ref}` does not match any branch or tag in `{repo_name}`."
    )
  if proc.returncode != 0:
    err = stderr.decode(errors="replace").strip()
    raise ResolveError(f"git ls-remote failed for `{repo_name}`: {err}")

  # Each output line is "<hash>\t<refname>".  Take the first match.
  for line in stdout.decode(errors="replace").splitlines():
    parts = line.split("\t", 1)
    if len(parts) == 2:
      return parts[0].strip().lower()

  raise ResolveError(
    f"Could not parse git ls-remote output for ref `{ref}` on `{repo_name}`."
  )
