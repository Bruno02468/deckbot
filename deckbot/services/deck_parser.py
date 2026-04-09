from __future__ import annotations

import hashlib
import re

import zstandard as zstd

from deckbot.models.deck import DeckProperties
from deckbot.models.sol import normalize_sol

# Matches the SOL line in the Executive Control Deck.
# Handles both numeric (SOL 101) and named (SOL SESTATICS) variants.
# The trailing comma/whitespace strip handles rare "SOL 101,0" syntax.
_SOL_RE = re.compile(r"^\s*SOL\s+(\S+)", re.IGNORECASE | re.MULTILINE)

# Matches the start of a GRID or GRID* bulk data entry in both fixed-field
# (first 8 chars are the field name, followed by a space) and free-field
# (field name terminated by comma) formats.
_GRID_RE = re.compile(r"^GRID\*?[ \t,]", re.IGNORECASE | re.MULTILINE)

_COMPRESSOR = zstd.ZstdCompressor(level=9)


def decode_deck(raw: bytes) -> str:
  """Decode raw deck bytes to text; tries UTF-8 then falls back to latin-1."""
  try:
    return raw.decode("utf-8")
  except UnicodeDecodeError:
    return raw.decode("latin-1")


def parse_deck(raw: bytes) -> DeckProperties:
  """Extract SOL type and GRID count from raw deck bytes."""
  text = decode_deck(raw)

  sol_match = _SOL_RE.search(text)
  raw_sol: str | None = None
  if sol_match:
    # Strip trailing commas (e.g. "101,0" → "101") before normalising.
    raw_sol = sol_match.group(1).rstrip(",")
  sol = normalize_sol(raw_sol)

  grid_count = len(_GRID_RE.findall(text))

  return DeckProperties(sol=sol, grid_count=grid_count)


def hash_deck(raw: bytes) -> str:
  """Return the SHA-256 hex digest of raw (uncompressed) deck bytes."""
  return hashlib.sha256(raw).hexdigest()


def compress_deck(raw: bytes) -> bytes:
  """Return zstd-compressed deck bytes."""
  return _COMPRESSOR.compress(raw)
