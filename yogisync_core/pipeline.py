from __future__ import annotations

import logging

from .collector_gmail import fetch_messages
from .config import Config
from .models import SyncResult
from .provider_detect import detect_provider
from .parsers.bonne import parse_bonne
from .parsers.yes_tokyo import parse_yes_tokyo
from .parsers.peatix import parse_peatix
from .parsers.mosh import parse_mosh
from .parsers.life_tuning import parse_life_tuning
from .store import EventStore
from .sync_gcal import upsert_event

logger = logging.getLogger(__name__)

PARSER_MAP = {
    "bonne": parse_bonne,
    "yes_tokyo": parse_yes_tokyo,
    "peatix": parse_peatix,
    "mosh": parse_mosh,
    "life_tuning": parse_life_tuning,
}


def run_sync(config: Config, limit: int = 50) -> SyncResult:
    result = SyncResult()
    store = EventStore(config.sqlite_path)
    try:
        messages = fetch_messages(config, limit=limit)
        for msg in messages:
            try:
                provider = detect_provider(msg)
                if not provider:
                    logger.info("skip: provider not detected (%s)", msg.subject)
                    result.skipped += 1
                    continue

                parser = PARSER_MAP.get(provider)
                if not parser:
                    logger.info("skip: parser not found (%s)", provider)
                    result.skipped += 1
                    continue

                event = parser(msg)
                if not event:
                    logger.info("skip: parse failed (%s)", provider)
                    result.skipped += 1
                    continue

                action, gcal_event_id = store.upsert_event(event)
                if action == "skipped":
                    result.skipped += 1
                    continue

                event_id = upsert_event(config, event, gcal_event_id)
                if event_id:
                    store.update_gcal_event_id(event.ensure_event_uid(), event_id)

                if action == "created":
                    result.created += 1
                else:
                    result.updated += 1

            except Exception:
                logger.exception("error processing message: %s", msg.id)
                result.errors += 1
    finally:
        store.close()

    return result
