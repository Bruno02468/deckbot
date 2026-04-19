from __future__ import annotations

from pydantic import BaseModel


class JobItem(BaseModel):
  """A pending run returned by ``GET /api/v1/jobs/next``.

  Consumed by both the API router (response model) and the node client.
  """

  run_id: int
  deck_id: int
  deck_filename: str
  # Raw (uncompressed) deck bytes encoded as base64.
  deck_content: str
  repo_name: str
  commit_hash: str
  repo_url: str


class CompleteMetadata(BaseModel):
  """Metadata sent by the node on ``POST /api/v1/jobs/{id}/complete``."""

  exit_code: int
  # "normal" | "fatal" | "crash" — derived by the node from the F06 + exit code.
  finish: str | None = None
  # Total number of valgrind error records from the XML; None = no XML.
  valgrind_errors: int | None = None
