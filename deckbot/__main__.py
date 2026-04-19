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

  api_parser = subparsers.add_parser("api", help="Start the API server")
  api_parser.add_argument(
    "--host",
    default="127.0.0.1",
    help="Host to bind (default: 127.0.0.1)",
  )
  api_parser.add_argument(
    "--port",
    type=int,
    default=8000,
    help="Port to bind (default: 8000)",
  )

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
  elif args.command == "api":
    import uvicorn

    uvicorn.run(
      "deckbot.api.app:app",
      host=args.host,
      port=args.port,
      log_level="info",
    )


if __name__ == "__main__":
  main()
