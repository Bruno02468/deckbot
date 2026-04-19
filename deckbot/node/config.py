from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NodeSettings(BaseSettings):
  """Configuration for the DeckBot compute node client.

  All settings are read from environment variables prefixed with ``NODE_``
  (or from a ``.env`` file in the working directory).

  Example ``.env`` entries::

      NODE_API_ENDPOINT=http://localhost:8000
      NODE_API_KEY=<key from /deckbot node-create>
      NODE_MAX_THREADS=2
      NODE_BUILD_CACHE_DIR=/var/lib/deckbot-node/cache
      NODE_WORK_BASE_DIR=/var/lib/deckbot-node/work
  """

  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_prefix="NODE_",
    extra="ignore",
  )

  api_endpoint: str = Field(
    description="Base URL of the DeckBot API, e.g. http://localhost:8000"
  )
  api_key: str = Field(description="API key issued by /deckbot node-create")
  max_threads: int = Field(
    default=1,
    ge=1,
    description="Maximum number of concurrent MYSTRAN runs",
  )
  build_cache_dir: Path = Field(
    default=Path("/tmp/deckbot_node/cache"),
    description="Directory for cloned repos and compiled MYSTRAN binaries",
  )
  work_base_dir: Path = Field(
    default=Path("/tmp/deckbot_node/work"),
    description="Parent directory for per-run temporary work directories",
  )
  # How often (seconds) to poll for new jobs when idle.
  poll_interval: float = Field(default=10.0, ge=1.0)
  # How often (seconds) to send a keepalive to the API.
  keepalive_interval: float = Field(default=60.0, ge=5.0)


_settings: NodeSettings | None = None


def get_node_settings() -> NodeSettings:
  global _settings
  if _settings is None:
    _settings = NodeSettings()
  return _settings
