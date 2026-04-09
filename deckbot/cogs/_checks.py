from __future__ import annotations

import discord
from discord import app_commands


def _is_deckbot_admin(interaction: discord.Interaction) -> bool:
  """True when the invoking member is a bot admin."""
  member = interaction.user
  if not isinstance(member, discord.Member):
    return False
  if interaction.guild and member.id == interaction.guild.owner_id:
    return True
  if member.guild_permissions.administrator:
    return True
  return any(r.name.lower() == "deckbot" for r in member.roles)


def admin_check(interaction: discord.Interaction) -> bool:
  if not _is_deckbot_admin(interaction):
    raise app_commands.CheckFailure(
      "You need the **deckbot** role or administrator permissions."
    )
  return True
