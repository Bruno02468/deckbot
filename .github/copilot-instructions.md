# DeckBot — Copilot Context

DeckBot is an async Discord bot that crawls and listens for FEA deck files
(`.bdf`, `.dat`, `.nas`) in tracked channels of the MYSTRAN Discord server,
stores them in a local SQLite database with deduplication and metadata, and
exposes slash commands for administration and queries.

This file contains a basic description of the bot and its features.

You can update this file if you make changes relevant to its content.

## Code style

- Python 3.12+, formatted with **ruff** (2-space indent, 79-char line length,
  double quotes, LF line endings). Follow these strictly.
- All non-trivial functions use type hints. Public data structures use
  **Pydantic v2** models. DB interaction uses **SQLAlchemy 2.0 async ORM**.

## Key libraries

| Library                           | Role                                                          |
| --------------------------------- | ------------------------------------------------------------- |
| `discord.py >= 2.3`               | Discord API, slash commands (`app_commands`), event listeners |
| `SQLAlchemy[asyncio] + aiosqlite` | Async ORM + SQLite driver                                     |
| `alembic`                         | DB schema migrations (`python -m deckbot migrate`)            |
| `pydantic` / `pydantic-settings`  | Data models + `.env` config                                   |
| `zstandard`                       | zstd compression of deck BLOBs                                |

## Project layout

```
deckbot/
  __main__.py      — CLI entry point: `run` (start bot) | `migrate` (alembic upgrade head)
  config.py        — pydantic-settings: DISCORD_TOKEN, DISCORD_GUILD_ID, DB_PATH
  bot.py           — DeckBot(commands.Bot): loads cogs, starts JobRunner, tracks channel IDs
  db/
    models.py      — SQLAlchemy ORM: Setting, Channel, Job, ProcessedMessage, Deck, DeckTag
    session.py     — async engine factory, get_session() context manager, enable_wal()
    queries.py     — async query helpers (list_decks, count_jobs_by_status, etc.)
  cogs/
    _checks.py     — shared permission helpers: _is_deckbot_admin(), admin_check()
    listener.py    — on_message: processes attachments in tracked channels in real time
    admin.py       — /deckbot group (admin-only): setup, track, untrack, channels, crawl, reprocess, status, jobs
    decks.py       — /deck group: list, search, tag, untag
  services/
    deck_parser.py — parse_deck() → DeckProperties (SolType | None, grid_count); hash_deck(); compress_deck()
    zip_handler.py — extract_decks(): recursive ZIP extraction, depth ≤ 3, ≤ 50 MB total
    processor.py   — process_message(): dedup guard, dispatches by extension, does NOT commit
    crawler.py     — crawl_channel(): iterates channel.history(), checkpoints every 100 messages
    job_runner.py  — JobRunner: background asyncio.Task, one job at a time, resets stale on start
    reprocessor.py — reprocess_channel(): re-parses stored BLOBs, updates sol + grid_count in batches
  models/
    deck.py        — DeckProperties, DeckInfo (Pydantic); sol field is SolType | None
    job.py         — CrawlChannelPayload, ReprocessChannelPayload (Pydantic, discriminated union)
    sol.py         — SolType (StrEnum), _SOL_ALIASES lookup table, normalize_sol()
migrations/        — Alembic versions (single migration: 0001_initial_schema)
alembic.ini
deckbot.service    — systemd unit for VPS deployment
.env.example
```

## Database schema (SQLite, WAL mode)

- **settings** `(key PK, value)` — runtime config (e.g. `deckbot_channel_id`)
- **channels** `(channel_id PK, guild_id, name, added_at, last_crawled_message_id)` — tracked channels; checkpoint stored here
- **jobs** `(id, type, status, payload JSON, created_at, updated_at, error)` — job queue; statuses: `pending / running / completed / failed`
- **processed_messages** `(message_id PK, channel_id, processed_at)` — prevents double-processing
- **decks** `(id, hash UNIQUE, filename, sol, grid_count, size_bytes, content BLOB, source_message_id, source_channel_id, source_url, discovered_at)` — `content` is zstd-compressed; `hash` is SHA-256 of raw bytes; `sol` stores a `SolType` string value (`NULL` = no SOL line; `"unknown"` = SOL line present but unrecognized)
- **deck_tags** `(deck_id, tag PK, tagged_by, tagged_at)` — predefined tags: `should_fatal`, `incompatible`, `bad_result`, `slow`, `big`

## Core behaviours to know

- **Deduplication**: SHA-256 of raw (uncompressed) content. Duplicates are silently skipped. `processor.py` also checks `session.new` to catch within-batch duplicates before the commit.
- **process_message** does NOT commit — the caller (listener or crawler) owns the transaction.
- **Listener** only fires for channels in `bot.tracked_channel_ids` (an in-memory `set[int]` loaded from DB on startup, refreshed after track/untrack).
- **JobRunner** polls every 30 s when idle, 2 s between jobs when busy. On startup it resets any `running` jobs to `pending`.
- **Crawler** resumes from `Channel.last_crawled_message_id` and checkpoints it every 100 messages.
- **Admin permission check**: guild owner OR `administrator` permission OR any role named `deckbot` (case-insensitive). Implemented in `cogs/_checks.py` and shared by both `admin.py` and `decks.py`.
- **Ephemeral responses**: all commands respond non-ephemerally inside the deckbot home channel, ephemerally everywhere else.
- **SOL normalization**: raw SOL strings are mapped to a canonical `SolType` via a lookup table in `models/sol.py`. `NULL` = no SOL line; `"unknown"` = SOL found but not in the table. Run `/deckbot reprocess <channel>` after any change to the lookup table.
- **JobRunner job types**: `crawl_channel` and `reprocess_channel`.
- All datetimes are UTC-aware (`datetime.now(UTC)`).

## What is NOT yet implemented

- Result files (`.op2`, `.f06`) — only decks are tracked for now
- Deck download / export command
