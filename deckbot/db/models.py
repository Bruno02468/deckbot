from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
  BigInteger,
  DateTime,
  ForeignKey,
  Integer,
  LargeBinary,
  String,
  Text,
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
