"""Add finish and valgrind_errors columns to runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.add_column(
    "runs",
    sa.Column("finish", sa.String(length=20), nullable=True),
  )
  op.add_column(
    "runs",
    sa.Column("valgrind_errors", sa.Integer(), nullable=True),
  )


def downgrade() -> None:
  op.drop_column("runs", "valgrind_errors")
  op.drop_column("runs", "finish")
