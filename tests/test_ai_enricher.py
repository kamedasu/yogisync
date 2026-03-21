from __future__ import annotations

from datetime import datetime

from yogisync_core.ai_enricher import EnrichResult, enrich_event
from yogisync_core.config import Config
from yogisync_core.models import Event, GmailMessage


def _config(tmp_db: str, *, yoga_enabled: bool = True, enrich_enabled: bool = True) -> Config:
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
        yoga_classifier_enabled=yoga_enabled,
        yoga_classifier_max_chars=2000,
        yoga_classifier_fail_open=False,
        yoga_classifier_min_confidence=0.6,
        yoga_classifier_keyword_fallback_enabled=True,
        yoga_classifier_keyword_fallback_terms="yoga,ヨガ",
        yoga_classifier_keyword_fallback_on_errors="insufficient_quota,rate_limit",
        yoga_classifier_target_providers="",
        ai_enricher_enabled=enrich_enabled,
        ai_enricher_target_providers="peatix",
        ai_enricher_max_chars_mail=800,
        ai_enricher_max_chars_source=1200,
        ai_enricher_http_timeout_sec=5,
        ai_enricher_model="gpt-5-mini",
        ai_enricher_fail_open=True,
        ai_enricher_cache_ttl_hours=24,
    )


def test_enrich_event_fills_only_missing(monkeypatch, tmp_path) -> None:
    config = _config(str(tmp_path / "test.db"))
    msg = GmailMessage(
        id="m1",
        subject="予約完了",
        snippet="ヨガクラス",
        text_plain="講師: 田中",
    )
    event = Event(
        provider="peatix",
        title="朝ヨガ",
        date=datetime(2026, 3, 15, 9, 0, 0),
        location_name=None,
        address="既存住所",
        instructor=None,
        source_url="https://example.com/event/1",
    )

    monkeypatch.setattr("yogisync_core.ai_enricher._get_cached_enrich", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("yogisync_core.ai_enricher._get_cached_or_fetch_source_text", lambda *_args, **_kwargs: "source")
    monkeypatch.setattr(
        "yogisync_core.ai_enricher.call_openai_enrich",
        lambda *_args, **_kwargs: EnrichResult(
            instructor="田中先生",
            location_name="渋谷スタジオ",
            address="AI住所",
            base_price="¥4,400",
            option_menus=["レンタルマット"],
            ticket_types=["一般"],
            notes="持ち物あり",
            confidence=0.9,
            evidence=["講師: 田中"],
        ),
    )

    enriched, filled, _ = enrich_event(config, msg, event)

    assert "instructor" in filled
    assert "location_name" in filled
    assert "address" not in filled
    assert enriched.instructor == "田中先生"
    assert enriched.location_name == "渋谷スタジオ"
    assert enriched.address == "既存住所"


def test_enrich_event_not_called_when_classifier_disabled(monkeypatch, tmp_path) -> None:
    config = _config(str(tmp_path / "test.db"), yoga_enabled=False, enrich_enabled=True)
    msg = GmailMessage(id="m1", subject="s", snippet="n", text_plain="body")
    event = Event(
        provider="peatix",
        title="朝ヨガ",
        date=datetime(2026, 3, 15, 9, 0, 0),
        source_url="https://example.com/event/1",
    )

    def _raise(*_args, **_kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr("yogisync_core.ai_enricher._get_cached_or_fetch_source_text", _raise)
    monkeypatch.setattr("yogisync_core.ai_enricher.call_openai_enrich", _raise)

    enriched, filled, enrich_result = enrich_event(config, msg, event)

    assert filled == []
    assert enrich_result is None
    assert enriched == event
