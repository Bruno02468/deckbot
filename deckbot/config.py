from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
  )

  discord_token: str = Field(description="Discord bot token")
  discord_guild_id: int = Field(description="Discord guild (server) ID")
  db_path: Path = Field(
    default=Path("deckbot.db"),
    description="Path to the SQLite database file",
  )
  api_public_url: str | None = Field(
    default=None,
    description=(
      "Public base URL of the DeckBot API server, e.g. "
      "https://myserver.com/deckbot_api — used to build F06/zip links "
      "in run embeds. Leave unset to omit file links."
    ),
  )


_settings: Settings | None = None


def get_settings() -> Settings:
  global _settings
  if _settings is None:
    _settings = Settings()
  return _settings
