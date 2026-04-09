"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
  op.create_table(
    "settings",
    sa.Column("key", sa.String(), nullable=False),
    sa.Column("value", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("key"),
  )

  op.create_table(
    "channels",
    sa.Column("channel_id", sa.BigInteger(), nullable=False),
    sa.Column("guild_id", sa.BigInteger(), nullable=False),
    sa.Column("name", sa.String(length=100), nullable=False),
    sa.Column(
      "added_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.Column("last_crawled_message_id", sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint("channel_id"),
  )

  op.create_table(
    "jobs",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("type", sa.String(length=50), nullable=False),
    sa.Column("status", sa.String(length=20), nullable=False),
    sa.Column("payload", sa.Text(), nullable=True),
    sa.Column(
      "created_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.Column(
      "updated_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.Column("error", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("id"),
  )

  op.create_table(
    "processed_messages",
    sa.Column("message_id", sa.BigInteger(), nullable=False),
    sa.Column("channel_id", sa.BigInteger(), nullable=False),
    sa.Column(
      "processed_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.PrimaryKeyConstraint("message_id"),
  )

  op.create_table(
    "decks",
    sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
    sa.Column("hash", sa.String(length=64), nullable=False),
    sa.Column("filename", sa.String(length=255), nullable=False),
    sa.Column("sol", sa.String(length=50), nullable=True),
    sa.Column("grid_count", sa.Integer(), nullable=False),
    sa.Column("size_bytes", sa.Integer(), nullable=False),
    sa.Column("content", sa.LargeBinary(), nullable=False),
    sa.Column("source_message_id", sa.BigInteger(), nullable=True),
    sa.Column("source_channel_id", sa.BigInteger(), nullable=True),
    sa.Column("source_url", sa.String(length=512), nullable=True),
    sa.Column(
      "discovered_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("hash"),
  )

  op.create_table(
    "deck_tags",
    sa.Column("deck_id", sa.Integer(), nullable=False),
    sa.Column("tag", sa.String(length=50), nullable=False),
    sa.Column("tagged_by", sa.BigInteger(), nullable=False),
    sa.Column(
      "tagged_at",
      sa.DateTime(timezone=True),
      server_default=sa.text("CURRENT_TIMESTAMP"),
      nullable=False,
    ),
    sa.ForeignKeyConstraint(["deck_id"], ["decks.id"]),
    sa.PrimaryKeyConstraint("deck_id", "tag"),
  )


def downgrade() -> None:
  op.drop_table("deck_tags")
  op.drop_table("decks")
  op.drop_table("processed_messages")
  op.drop_table("jobs")
  op.drop_table("channels")
  op.drop_table("settings")
