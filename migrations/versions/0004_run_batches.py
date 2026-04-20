"""Add run_batches table and batch_id/run_started_at to runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "run_batches",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("version_id", sa.Integer(), nullable=False),
    sa.Column("submitted_by", sa.BigInteger(), nullable=False),
    sa.Column("label", sa.String(length=200), nullable=True),
    sa.Column("filter_summary", sa.Text(), nullable=True),
    sa.Column(
      "created_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.ForeignKeyConstraint(["version_id"], ["mystran_versions.id"]),
    sa.PrimaryKeyConstraint("id"),
  )

  op.add_column(
    "runs",
    sa.Column("batch_id", sa.Integer(), nullable=True),
  )
  op.add_column(
    "runs",
    sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
  )


def downgrade() -> None:
  op.drop_column("runs", "run_started_at")
  op.drop_column("runs", "batch_id")
  op.drop_table("run_batches")
