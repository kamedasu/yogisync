from __future__ import annotations

import logging

from .ai_enricher import enrich_event
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
from .sync_gcal import reconcile_event
from .yoga_classifier import classify_yoga_event

logger = logging.getLogger(__name__)

PARSER_MAP = {
    "bonne": parse_bonne,
    "yes_tokyo": parse_yes_tokyo,
    "peatix": parse_peatix,
    "mosh": parse_mosh,
    "life_tuning": parse_life_tuning,
}


def _parse_provider_list(value: str) -> set[str]:
    return {p.strip().lower() for p in (value or "").split(",") if p.strip()}


def _is_enricher_target_provider(config: Config, provider: str) -> bool:
    targets = _parse_provider_list(getattr(config, "ai_enricher_target_providers", "") or "")
    if not targets:
        return False
    return (provider or "").strip().lower() in targets


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
                        len(msg.text_html or ""),
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
                        "skip: parse failed (%s) subject=%s from=%s snippet=%s plain_len=%s html_len=%s",
                        provider,
                        msg.subject,
                        msg.from_email,
                        (msg.snippet or "")[:80],
                        len(msg.text_plain or ""),
                        len(msg.text_html or ""),
                    )
                    result.skipped += 1
                    continue

                decision = classify_yoga_event(config, msg, event)
                logger.info(
                    "yoga_classifier: provider=%s subject=%s title=%s is_yoga=%s confidence=%.2f reason=%s signals=%s",
                    event.provider,
                    msg.subject,
                    event.title,
                    decision.is_yoga,
                    decision.confidence,
                    decision.reason,
                    decision.matched_signals,
                )

                is_yoga = decision.is_yoga
                if is_yoga and decision.confidence < config.yoga_classifier_min_confidence:
                    is_yoga = False
                    logger.info(
                        "yoga_classifier: low confidence -> treat as non-yoga provider=%s subject=%s title=%s confidence=%.2f threshold=%.2f",
                        event.provider,
                        msg.subject,
                        event.title,
                        decision.confidence,
                        config.yoga_classifier_min_confidence,
                    )

                if not is_yoga:
                    logger.info(
                        "skip: non-yoga event provider=%s subject=%s title=%s confidence=%.2f reason=%s",
                        event.provider,
                        msg.subject,
                        event.title,
                        decision.confidence,
                        decision.reason,
                    )
                    result.skipped += 1
                    continue

                # AIメタ補完（parse/yoga_classifierの後、storeの前）
                # 必須条件:
                # - YOGA_CLASSIFIER_ENABLED=true
                # - AI_ENRICHER_ENABLED=true
                # - provider が AI_ENRICHER_TARGET_PROVIDERS に含まれる
                if (
                    config.yoga_classifier_enabled
                    and getattr(config, "ai_enricher_enabled", False)
                    and _is_enricher_target_provider(config, event.provider)
                ):
                    try:
                        enriched_event, filled_fields, enrich_result = enrich_event(config, msg, event)
                        event = enriched_event
                        logger.info(
                            "ai_enricher: applied provider=%s source_url=%s filled_fields=%s confidence=%s",
                            event.provider,
                            event.source_url,
                            filled_fields,
                            None if enrich_result is None else enrich_result.confidence,
                        )
                    except Exception as exc:
                        logger.exception(
                            "ai_enricher: skipped due to error provider=%s source_url=%s err=%s",
                            event.provider,
                            event.source_url,
                            exc,
                        )
                else:
                    logger.info(
                        "ai_enricher: not_applied provider=%s enabled=%s yoga_enabled=%s target=%s",
                        event.provider,
                        getattr(config, "ai_enricher_enabled", False),
                        config.yoga_classifier_enabled,
                        _is_enricher_target_provider(config, event.provider),
                    )

                action, gcal_event_id = store.upsert_event(event)

                # ★重要:
                # - action=="skipped" でも、カレンダー側に “同一event_uid重複” が残ってる可能性がある
                # - なので reconcile_event を実行して、余分を削除して「残す1件」を確定させる
                if action == "skipped":
                    kept_id = reconcile_event(
                        config,
                        event,
                        gcal_event_id,
                        allow_create=False,       # skipped の時は新規作成しない
                        cleanup_duplicates=True,  # 重複掃除はする
                    )
                    if kept_id and kept_id != gcal_event_id:
                        store.update_gcal_event_id(event.ensure_event_uid(), kept_id)
                        logger.info(
                            "pipeline: gcal_event_id changed after reconcile event_uid=%s old=%s new=%s",
                            event.ensure_event_uid(),
                            gcal_event_id,
                            kept_id,
                        )

                    logger.info(
                        "skip: store skipped (%s) event_uid=%s gcal_event_id=%s subject=%s",
                        provider,
                        event.ensure_event_uid(),
                        gcal_event_id,
                        msg.subject,
                    )
                    result.skipped += 1
                    continue

                # created/updated の場合は “必ず reconcile” を通して、二重作成を避ける
                kept_id = reconcile_event(
                    config,
                    event,
                    gcal_event_id,
                    allow_create=True,
                    cleanup_duplicates=True,
                )

                if kept_id:
                    store.update_gcal_event_id(event.ensure_event_uid(), kept_id)

                if action == "created":
                    result.created += 1
                else:
                    result.updated += 1

            except Exception:
                logger.exception("error processing message: %s", msg.id)
                result.errors += 1

    finally:
        store.close()

    logger.info("pipeline: result=%s", result)
    return result
