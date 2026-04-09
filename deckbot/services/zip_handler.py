from __future__ import annotations

import io
import logging
import zipfile
from pathlib import PurePosixPath

log = logging.getLogger(__name__)

DECK_EXTENSIONS: frozenset[str] = frozenset({".bdf", ".dat", ".nas"})
_ZIP_EXTENSION = ".zip"

# Zip-bomb protection parameters.
_MAX_DEPTH = 3
_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB


class _BudgetTracker:
  """Tracks total extracted bytes across a recursive extraction run."""

  __slots__ = ("remaining",)

  def __init__(self, max_bytes: int) -> None:
    self.remaining = max_bytes

  def consume(self, n: int) -> bool:
    """Subtract n from the budget. Returns False when budget is exhausted."""
    self.remaining -= n
    return self.remaining >= 0


def extract_decks(
  data: bytes,
  *,
  depth: int = 0,
  _budget: _BudgetTracker | None = None,
) -> list[tuple[str, bytes]]:
  """Recursively extract deck files from a ZIP archive.

  Returns a list of (filename, raw_bytes) pairs. Filenames are the bare
  basenames of the entries (not full paths inside the archive).

  Zip-bomb protection: stops when total extracted bytes would exceed
  _MAX_TOTAL_BYTES or when recursion depth exceeds _MAX_DEPTH.
  """
  if depth > _MAX_DEPTH:
    log.warning("ZIP recursion depth %d exceeded; stopping extraction", depth)
    return []

  if _budget is None:
    _budget = _BudgetTracker(_MAX_TOTAL_BYTES)

  results: list[tuple[str, bytes]] = []

  try:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
      for entry in zf.infolist():
        if entry.is_dir():
          continue

        name = entry.filename
        ext = _extension(name)

        if ext == _ZIP_EXTENSION:
          nested = zf.read(name)
          if not _budget.consume(len(nested)):
            log.warning("ZIP extraction budget exceeded at %s; stopping", name)
            return results
          results.extend(
            extract_decks(nested, depth=depth + 1, _budget=_budget)
          )
        elif ext in DECK_EXTENSIONS:
          raw = zf.read(name)
          if not _budget.consume(len(raw)):
            log.warning("ZIP extraction budget exceeded at %s; stopping", name)
            return results
          # Use only the basename — we don't care about the archive path.
          basename = PurePosixPath(name).name
          results.append((basename, raw))

  except zipfile.BadZipFile:
    log.warning("Could not open ZIP: bad or corrupt file")

  return results


def _extension(filename: str) -> str:
  """Return the lowercase file extension including the dot, or ''."""
  dot = filename.rfind(".")
  return filename[dot:].lower() if dot != -1 else ""
