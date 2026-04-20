from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from deckbot.api.auth import require_active_node
from deckbot.api.deps import get_db_session
from deckbot.db.models import Node
from deckbot.db.queries import get_setting, reset_orphaned_runs

router = APIRouter(tags=["nodes"])

log = logging.getLogger(__name__)

_DEFAULT_ORPHAN_GRACE = 180  # seconds


class KeepaliveBody(BaseModel):
  max_threads: int | None = None
  # Run IDs this node is currently working on.  When present (even as an
  # empty list) the server reconciles DB state and resets ghost runs.
  # None (field absent) = old client — skip reconciliation entirely to
  # avoid incorrectly resetting in-progress builds on old nodes.
  running_run_ids: list[int] | None = None


@router.post("/keepalive", status_code=204)
async def keepalive(
  body: KeepaliveBody,
  node: Node = Depends(require_active_node),
  session: AsyncSession = Depends(get_db_session),
) -> None:
  """Node keep-alive endpoint.

  Updates ``last_seen_at`` and the self-reported ``max_threads`` for the
  calling node.  When the node reports ``running_run_ids``, reconciles DB
  state: any run this node owns that is not in the list (and is older than
  the grace period) is reset to ``pending`` for re-execution.
  Returns 204 No Content on success.
  """
  node.last_seen_at = datetime.now(UTC)
  if body.max_threads is not None:
    node.max_threads = body.max_threads

  # ── Ghost-run reconciliation ─────────────────────────────────────────────
  # Only reconcile when the node explicitly sends running_run_ids.  A None
  # value means the field was absent (old node client) — skip to avoid
  # resetting legitimately in-progress builds that the old client doesn't
  # report.  An empty list [] from a new client means "nothing running".
  if body.running_run_ids is not None:
    grace_raw = await get_setting(session, "run_orphan_grace_seconds")
    grace = int(grace_raw) if grace_raw else _DEFAULT_ORPHAN_GRACE

    reset_ids = await reset_orphaned_runs(
      session,
      node_id=node.id,
      reported_run_ids=body.running_run_ids,
      grace_seconds=grace,
    )
    if reset_ids:
      log.warning(
        "Node '%s' keepalive: reset %d ghost run(s) to pending: %s",
        node.name,
        len(reset_ids),
        reset_ids,
      )

  await session.commit()
