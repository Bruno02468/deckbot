from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
  BigInteger,
  Boolean,
  DateTime,
  ForeignKey,
  Integer,
  LargeBinary,
  String,
  Text,
  UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
  return datetime.now(UTC)


class Base(DeclarativeBase):
  pass


class Setting(Base):
  __tablename__ = "settings"

  key: Mapped[str] = mapped_column(String, primary_key=True)
  value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Channel(Base):
  __tablename__ = "channels"

  channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
  guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
  name: Mapped[str] = mapped_column(String(100), nullable=False)
  added_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )
  last_crawled_message_id: Mapped[int | None] = mapped_column(
    BigInteger, nullable=True
  )


class Job(Base):
  __tablename__ = "jobs"

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  type: Mapped[str] = mapped_column(String(50), nullable=False)
  status: Mapped[str] = mapped_column(
    String(20), nullable=False, default="pending"
  )
  # JSON-encoded payload specific to the job type.
  payload: Mapped[str | None] = mapped_column(Text, nullable=True)
  created_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )
  updated_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    default=_utcnow,
    onupdate=_utcnow,
    nullable=False,
  )
  error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProcessedMessage(Base):
  __tablename__ = "processed_messages"

  message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
  channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
  processed_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )


class Deck(Base):
  __tablename__ = "decks"

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  # SHA-256 hex digest of the raw (uncompressed) content.
  hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
  filename: Mapped[str] = mapped_column(String(255), nullable=False)
  sol: Mapped[str | None] = mapped_column(String(50), nullable=True)
  grid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
  # Original uncompressed size in bytes.
  size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
  # zstd-compressed deck content stored as a BLOB.
  content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
  source_message_id: Mapped[int | None] = mapped_column(
    BigInteger, nullable=True
  )
  source_channel_id: Mapped[int | None] = mapped_column(
    BigInteger, nullable=True
  )
  source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
  discovered_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )

  tags: Mapped[list[DeckTag]] = relationship("DeckTag", back_populates="deck")
  runs: Mapped[list[Run]] = relationship("Run", back_populates="deck")


class DeckTag(Base):
  __tablename__ = "deck_tags"

  deck_id: Mapped[int] = mapped_column(
    Integer, ForeignKey("decks.id"), primary_key=True
  )
  tag: Mapped[str] = mapped_column(String(50), primary_key=True)
  tagged_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
  tagged_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )

  deck: Mapped[Deck] = relationship("Deck", back_populates="tags")


class MystranVersion(Base):
  __tablename__ = "mystran_versions"
  __table_args__ = (UniqueConstraint("repo_name", "commit_hash"),)

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  # Key into the APPROVED_REPOS constant dict.
  repo_name: Mapped[str] = mapped_column(String(100), nullable=False)
  # Full 40-char SHA-1.
  commit_hash: Mapped[str] = mapped_column(String(40), nullable=False)
  # Branch or tag name used at resolution time (informational).
  ref_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
  resolved_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )

  runs: Mapped[list[Run]] = relationship("Run", back_populates="version")


class Node(Base):
  __tablename__ = "nodes"

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
  # SHA-256 hex digest of the raw API key. Key is shown once at creation.
  api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
  created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
  created_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )
  # Updated on every keepalive.
  last_seen_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  # Self-reported by the node on keepalive; informational only.
  max_threads: Mapped[int | None] = mapped_column(Integer, nullable=True)
  is_active: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=True
  )

  runs: Mapped[list[Run]] = relationship("Run", back_populates="node")


class Run(Base):
  __tablename__ = "runs"

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  deck_id: Mapped[int] = mapped_column(
    Integer, ForeignKey("decks.id"), nullable=False
  )
  version_id: Mapped[int] = mapped_column(
    Integer, ForeignKey("mystran_versions.id"), nullable=False
  )
  # pending / running / completed / failed / cancelled
  status: Mapped[str] = mapped_column(
    String(20), nullable=False, default="pending"
  )
  # Assigned when the run is claimed by a node.
  node_id: Mapped[int | None] = mapped_column(
    Integer, ForeignKey("nodes.id"), nullable=True
  )
  # Discord snowflake of the user who submitted the run.
  submitted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
  created_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )
  started_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  completed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
  error: Mapped[str | None] = mapped_column(Text, nullable=True)

  deck: Mapped[Deck] = relationship("Deck", back_populates="runs")
  version: Mapped[MystranVersion] = relationship(
    "MystranVersion", back_populates="runs"
  )
  node: Mapped[Node | None] = relationship("Node", back_populates="runs")
  files: Mapped[list[RunFile]] = relationship("RunFile", back_populates="run")


class RunFile(Base):
  __tablename__ = "run_files"

  id: Mapped[int] = mapped_column(
    Integer, primary_key=True, autoincrement=True
  )
  run_id: Mapped[int] = mapped_column(
    Integer, ForeignKey("runs.id"), nullable=False
  )
  filename: Mapped[str] = mapped_column(String(255), nullable=False)
  # zstd-compressed file content.
  content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
  # Original uncompressed size in bytes.
  size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
  stored_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), default=_utcnow, nullable=False
  )

  run: Mapped[Run] = relationship("Run", back_populates="files")
