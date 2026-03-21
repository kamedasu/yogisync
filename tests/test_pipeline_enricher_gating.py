from __future__ import annotations

from datetime import datetime

from yogisync_core.config import Config
from yogisync_core.models import Event, GmailMessage, YogaDecision
import yogisync_core.pipeline as pipeline_module
from yogisync_core.pipeline import run_sync


def _config(tmp_db: str) -> Config:
    return Config(
        gmail_query="newer_than:365d",
        google_client_secret_path="client_secret.json",
        google_token_path="token.json",
        yogisync_calendar_id="",
        timezone="Asia/Tokyo",
        sqlite_path=tmp_db,
        default_event_duration_minutes=60,
        openai_api_key="test-key",
        openai_model_yoga_classifier="gpt-5-mini",
        yoga_classifier_enabled=False,
        yoga_classifier_max_chars=2000,
        yoga_classifier_fail_open=False,
        yoga_classifier_min_confidence=0.6,
        yoga_classifier_keyword_fallback_enabled=True,
        yoga_classifier_keyword_fallback_terms="yoga,ヨガ",
        yoga_classifier_keyword_fallback_on_errors="insufficient_quota,rate_limit",
        yoga_classifier_target_providers="",
        ai_enricher_enabled=True,
        ai_enricher_target_providers="peatix",
        ai_enricher_max_chars_mail=800,
        ai_enricher_max_chars_source=1200,
        ai_enricher_http_timeout_sec=5,
        ai_enricher_model="gpt-5-mini",
        ai_enricher_fail_open=True,
        ai_enricher_cache_ttl_hours=24,
    )


def test_pipeline_does_not_call_enricher_when_classifier_disabled(monkeypatch, tmp_path) -> None:
    config = _config(str(tmp_path / "pipeline.db"))
    msg = GmailMessage(id="m1", subject="予約", snippet="snippet", text_plain="body")
    event = Event(
        provider="peatix",
        title="朝ヨガ",
        date=datetime(2026, 3, 15, 9, 0, 0),
        source_url="https://example.com/e/1",
    )

    monkeypatch.setattr("yogisync_core.pipeline.fetch_messages", lambda *_args, **_kwargs: [msg])
    monkeypatch.setattr("yogisync_core.pipeline.detect_provider", lambda *_args, **_kwargs: "peatix")
    monkeypatch.setitem(pipeline_module.PARSER_MAP, "peatix", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(
        "yogisync_core.pipeline.classify_yoga_event",
        lambda *_args, **_kwargs: YogaDecision(
            is_yoga=True,
            confidence=1.0,
            reason="test",
            matched_signals=["test"],
        ),
    )
    monkeypatch.setattr("yogisync_core.pipeline.reconcile_event", lambda *_args, **_kwargs: None)

    def _raise(*_args, **_kwargs):
        raise AssertionError("enricher must not be called")

    monkeypatch.setattr("yogisync_core.pipeline.enrich_event", _raise)

    result = run_sync(config, limit=1)

    assert result.created == 1
    assert result.errors == 0
