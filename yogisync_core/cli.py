from __future__ import annotations

import argparse
import logging
import os

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
logger.info("cli: logger alive (after basicConfig)")

from .config import load_config
from .pipeline import run_sync


def main() -> None:
    print("cli: print alive")
    parser = argparse.ArgumentParser(description="YogiSync local sync")
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Sync Gmail to Google Calendar")
    sync_parser.add_argument("--limit", type=int, default=50, help="Max messages to fetch")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "sync":
        config = load_config()
        result = run_sync(config, limit=args.limit)
        print(result.model_dump_json())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
