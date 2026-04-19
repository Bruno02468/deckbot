"""Add runs schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "mystran_versions",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("repo_name", sa.String(length=100), nullable=False),
    sa.Column("commit_hash", sa.String(length=40), nullable=False),
    sa.Column("ref_name", sa.String(length=255), nullable=True),
    sa.Column(
      "resolved_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("repo_name", "commit_hash"),
  )

  op.create_table(
    "nodes",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("name", sa.String(length=100), nullable=False),
    sa.Column("api_key_hash", sa.String(length=64), nullable=False),
    sa.Column("created_by", sa.BigInteger(), nullable=False),
    sa.Column(
      "created_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("max_threads", sa.Integer(), nullable=True),
    sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("name"),
  )

  op.create_table(
    "runs",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("deck_id", sa.Integer(), nullable=False),
    sa.Column("version_id", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(length=20), nullable=False),
    sa.Column("node_id", sa.Integer(), nullable=True),
    sa.Column("submitted_by", sa.BigInteger(), nullable=False),
    sa.Column(
      "created_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("exit_code", sa.Integer(), nullable=True),
    sa.Column("error", sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(["deck_id"], ["decks.id"]),
    sa.ForeignKeyConstraint(["version_id"], ["mystran_versions.id"]),
    sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
    sa.PrimaryKeyConstraint("id"),
  )

  op.create_table(
    "run_files",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("run_id", sa.Integer(), nullable=False),
    sa.Column("filename", sa.String(length=255), nullable=False),
    sa.Column("content", sa.LargeBinary(), nullable=False),
    sa.Column("size_bytes", sa.Integer(), nullable=False),
    sa.Column(
      "stored_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
    sa.PrimaryKeyConstraint("id"),
  )


def downgrade() -> None:
  op.drop_table("run_files")
  op.drop_table("runs")
  op.drop_table("nodes")
  op.drop_table("mystran_versions")
