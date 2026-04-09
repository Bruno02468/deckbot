import argparse
import asyncio


def main() -> None:
  parser = argparse.ArgumentParser(
    prog="deckbot",
    description="MYSTRAN deck-archiving Discord bot",
  )
  subparsers = parser.add_subparsers(dest="command", required=True)
  subparsers.add_parser("run", help="Start the bot")
  subparsers.add_parser("migrate", help="Apply database migrations")
  args = parser.parse_args()

  if args.command == "run":
    from deckbot.bot import run_bot

    try:
      asyncio.run(run_bot())
    except KeyboardInterrupt:
      pass
  elif args.command == "migrate":
    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    alembic_command.upgrade(cfg, "head")


if __name__ == "__main__":
  main()
