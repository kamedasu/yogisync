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
    logger.info("pipeline: sqlite_path=%s", config.sqlite_path)
    try:
        messages = fetch_messages(config, limit=limit)
        for msg in messages:
            logger.info("pipeline: processing msg id=%s subject=%s", msg.id, msg.subject)

            try:
                provider = detect_provider(msg)
                if not provider:
                    logger.info(
                        "skip: provider not detected subject=%s from=%s snippet=%s plain_len=%s html_len=%s",
                        msg.subject,
                        msg.from_email,
                        (msg.snippet or "")[:80],
                        len(msg.text_plain or ""),
                        len(msg.text_html or "")
                    )
                    result.skipped += 1
                    continue

                parser = PARSER_MAP.get(provider)
                if not parser:
                    logger.info("skip: parser not found (%s)", provider)
                    result.skipped += 1
                    continue

                event = parser(msg)
                if not event:
                    logger.info(
                        "skip: parser not found (%s) subject=%s from=%s snippet=%s plain_len=%s html_len=%s",
                        provider,
                        msg.subject,
                        msg.from_email,
                        (msg.snippet or "")[:80],
                        len(msg.text_plain or ""),
                        len(msg.text_html or ""),
                    )
                    result.skipped += 1
                    continue

                action, gcal_event_id = store.upsert_event(event)
                if action == "skipped":
                    logger.info(
                        "skip: store skipped (%s) event_uid=%s gcal_event_id=%s subject=%s",
                        provider,
                        event.ensure_event_uid(),
                        gcal_event_id,
                        msg.subject,
                    )
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

    logging.getLogger(__name__).info("pipeline: result=%s", result)
    return result
