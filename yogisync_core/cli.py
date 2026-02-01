from __future__ import annotations

import argparse
import logging

from .config import load_config
from .pipeline import run_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="YogiSync local sync")
    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Sync Gmail to Google Calendar")
    sync_parser.add_argument("--limit", type=int, default=50, help="Max messages to fetch")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "sync":
        config = load_config()
        result = run_sync(config, limit=args.limit)
        print(result.json())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
