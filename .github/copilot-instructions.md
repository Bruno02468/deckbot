# DeckBot — Copilot Context

DeckBot is an async Discord bot that crawls and listens for FEA deck files
(`.bdf`, `.dat`, `.nas`) in tracked channels of the MYSTRAN Discord server,
stores them in a local SQLite database with deduplication and metadata, and
exposes slash commands for administration and queries. It also supports
distributed MYSTRAN run submission via volunteer compute nodes.

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
| `fastapi` + `uvicorn[standard]`   | REST API consumed by compute nodes                            |
| `httpx`                           | Async HTTP client used by the node                            |

## Project layout

```
deckbot/
  __main__.py      — CLI entry point: `run` | `migrate` | `api` [--host --port] | `node`
  config.py        — pydantic-settings: DISCORD_TOKEN, DISCORD_GUILD_ID, DB_PATH
  bot.py           — DeckBot(commands.Bot): loads cogs, starts JobRunner, tracks channel IDs
  db/
    models.py      — SQLAlchemy ORM: Setting, Channel, Job, ProcessedMessage, Deck, DeckTag,
                     MystranVersion, Node, Run, RunFile
    session.py     — async engine factory, get_session() context manager, enable_wal()
    queries.py     — async query helpers (list_decks, count_jobs_by_status, etc.)
  cogs/
    _checks.py     — shared permission helpers: _is_deckbot_admin(), admin_check()
    listener.py    — on_message: processes attachments in tracked channels in real time
    admin.py       — /deckbot group (admin-only): setup, track, untrack, channels, crawl,
                     reprocess, status, jobs, node-create, node-list, node-remove
    decks.py       — /deck group: list, search, tag, untag, run, run-bulk, repos, runs,
                     run-status
  services/
    deck_parser.py — parse_deck() → DeckProperties (SolType | None, grid_count); hash_deck(); compress_deck()
    zip_handler.py — extract_decks(): recursive ZIP extraction, depth ≤ 3, ≤ 50 MB total
    processor.py   — process_message(): dedup guard, dispatches by extension, does NOT commit
    crawler.py     — crawl_channel(): iterates channel.history(), checkpoints every 100 messages
    job_runner.py  — JobRunner: background asyncio.Task, one job at a time, resets stale on start
    reprocessor.py — reprocess_channel(): re-parses stored BLOBs, updates sol + grid_count in batches
    version_resolver.py — resolve_ref(): resolves branch/tag/commit ref to full SHA-1 via git ls-remote
  models/
    deck.py        — DeckProperties, DeckInfo (Pydantic); sol field is SolType | None
    job.py         — CrawlChannelPayload, ReprocessChannelPayload (Pydantic, discriminated union)
    sol.py         — SolType (StrEnum), _SOL_ALIASES lookup table, normalize_sol()
    repo.py        — APPROVED_REPOS: dict[str, str] — static map of short name → git URL
    run.py         — JobItem, CompleteMetadata (Pydantic); shared by API and node client
  api/
    app.py         — FastAPI app factory; lifespan calls enable_wal()
    auth.py        — require_active_node dependency: X-API-Key header → SHA-256 → nodes table
    deps.py        — get_db_session dependency
    routers/
      nodes.py     — POST /api/v1/keepalive: updates last_seen_at + max_threads, returns 204
      jobs.py      — GET /api/v1/jobs/next?slots=N; POST /api/v1/jobs/{id}/complete (multipart);
                     POST /api/v1/jobs/{id}/fail
  node/
    config.py      — NodeSettings (pydantic-settings, NODE_ prefix): api_endpoint, api_key,
                     max_threads, build_cache_dir, work_base_dir, poll_interval, keepalive_interval
    builder.py     — get_binary(): clone/fetch → clean → checkout → cmake Debug build → binary cache
    sandbox.py     — build_command(): firejail --net=none + valgrind memcheck argv list
    runner.py      — run_job(): write deck → get binary → sandbox → collect outputs → multipart upload
    client.py      — NodeClient: asyncio keepalive loop + job poll loop
migrations/        — Alembic versions: 0001_initial_schema, 0002_runs_schema
alembic.ini
deckbot.service    — systemd unit for the bot (VPS)
deckbot-api.service — systemd unit for the API server (VPS)
deckbot-node.service — systemd unit template for volunteer compute nodes
.env.example
```

## Database schema (SQLite, WAL mode)

- **settings** `(key PK, value)` — runtime config (e.g. `deckbot_channel_id`, `node_timeout_seconds`, `max_artifact_bytes`)
- **channels** `(channel_id PK, guild_id, name, added_at, last_crawled_message_id)` — tracked channels; checkpoint stored here
- **jobs** `(id, type, status, payload JSON, created_at, updated_at, error)` — job queue; statuses: `pending / running / completed / failed`
- **processed_messages** `(message_id PK, channel_id, processed_at)` — prevents double-processing
- **decks** `(id, hash UNIQUE, filename, sol, grid_count, size_bytes, content BLOB, source_message_id, source_channel_id, source_url, discovered_at)` — `content` is zstd-compressed; `hash` is SHA-256 of raw bytes; `discovered_at` is the Discord message timestamp
- **deck_tags** `(deck_id, tag PK, tagged_by, tagged_at)` — predefined tags: `should_fatal`, `incompatible`, `bad_result`, `slow`, `big`
- **mystran_versions** `(id, repo_name, commit_hash, ref_name, UNIQUE(repo_name, commit_hash))` — resolved MYSTRAN versions
- **nodes** `(id, name UNIQUE, api_key_hash, last_seen_at, max_threads, is_active)` — registered compute nodes; `api_key_hash` is SHA-256 of the raw key
- **runs** `(id, deck_id FK, version_id FK, node_id FK, status, submitted_by, created_at, started_at, completed_at, exit_code, error)` — run queue; statuses: `pending / running / completed / failed / cancelled`
- **run_files** `(id, run_id FK, filename, content BLOB, size_bytes, stored_at)` — zstd-compressed output files from a completed run

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
- All datetimes are UTC-aware (`datetime.now(UTC)`). SQLite returns naive datetimes; apply `.replace(tzinfo=UTC)` before arithmetic.
- **Run submission**: manual only (no auto-run on crawl). `/deck run` submits a single deck; `/deck run-bulk` submits all decks matching a search query (requires confirmation if ≥ 50 decks).
- **Approved repos**: static dict in `models/repo.py`. Currently: `{"mystran": "https://github.com/MYSTRANsolver/MYSTRAN.git"}`. Version refs are resolved to full SHA-1 via `git ls-remote`.
- **Node API auth**: `X-API-Key` header; key is SHA-256-hashed before storage. Key is shown exactly once at node creation (`/deckbot node-create`).
- **Binary cache**: nodes cache built binaries at `{build_cache_dir}/binaries/{repo_name}/{commit_hash}/mystran`. Build uses CMake `Debug` mode. Shared repo clone lives at `{build_cache_dir}/repos/{repo_name}/`. One asyncio.Lock per repo prevents concurrent builds.
- **Sandbox**: each run executes `valgrind --tool=memcheck --xml=yes --track-origins=yes --read-inline-info=yes --read-var-info=yes --expensive-definedness-checks=yes {binary} {deck}` with `cwd` set to an isolated work directory. firejail is **not** used (incompatible with valgrind). All output files (excluding the input deck) are uploaded to the API as multipart on completion.

## What is NOT yet implemented

- Result files (`.op2`, `.f06`) — only decks are tracked for now
- Deck download / export command
