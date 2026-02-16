from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, Optional

from dotenv import load_dotenv


class SettingsSource(Protocol):
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        ...


@dataclass
class EnvSource:
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(key, default)


@dataclass
class Config:
    gmail_query: str
    google_client_secret_path: str
    google_token_path: str
    yogisync_calendar_id: str
    timezone: str
    sqlite_path: str
    default_event_duration_minutes: int
    openai_api_key: Optional[str]
    openai_model_yoga_classifier: str
    yoga_classifier_enabled: bool
    yoga_classifier_max_chars: int
    yoga_classifier_fail_open: bool
    yoga_classifier_min_confidence: float
    yoga_classifier_keyword_fallback_enabled: bool
    yoga_classifier_keyword_fallback_terms: str
    yoga_classifier_keyword_fallback_on_errors: str


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    s = value.strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_float(value: Optional[str], default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def load_config(source: Optional[SettingsSource] = None, dotenv_path: Optional[str] = None) -> Config:
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()

    src = source or EnvSource()

    gmail_query = src.get("GMAIL_QUERY") or src.get("gmail_query") or "newer_than:365d"
    google_client_secret_path = (
        src.get("GOOGLE_CLIENT_SECRET_PATH")
        or src.get("google_client_secret_path")
        or "client_secret.json"
    )
    google_token_path = (
        src.get("GOOGLE_TOKEN_PATH") or src.get("google_token_path") or "token.json"
    )
    yogisync_calendar_id = (
        src.get("YOGISYNC_CALENDAR_ID")
        or src.get("yogisync_calendar_id")
        or ""
    )
    timezone = src.get("TIMEZONE") or src.get("timezone") or "Asia/Tokyo"
    sqlite_path = src.get("SQLITE_PATH") or src.get("sqlite_path") or "data/yogisync.db"
    default_event_duration_minutes = _parse_int(
        src.get("DEFAULT_EVENT_DURATION_MINUTES")
        or src.get("default_event_duration_minutes"),
        60,
    )

    openai_api_key = src.get("OPENAI_API_KEY") or src.get("openai_api_key")
    openai_model_yoga_classifier = (
        src.get("OPENAI_MODEL_YOGA_CLASSIFIER")
        or src.get("openai_model_yoga_classifier")
        or "gpt-5-mini"
    )
    yoga_classifier_enabled = _parse_bool(
        src.get("YOGA_CLASSIFIER_ENABLED") or src.get("yoga_classifier_enabled"),
        True,
    )
    yoga_classifier_max_chars = _parse_int(
        src.get("YOGA_CLASSIFIER_MAX_CHARS") or src.get("yoga_classifier_max_chars"),
        2000,
    )
    yoga_classifier_fail_open = _parse_bool(
        src.get("YOGA_CLASSIFIER_FAIL_OPEN") or src.get("yoga_classifier_fail_open"),
        False,
    )
    yoga_classifier_min_confidence = _parse_float(
        src.get("YOGA_CLASSIFIER_MIN_CONFIDENCE")
        or src.get("yoga_classifier_min_confidence"),
        0.60,
    )
    yoga_classifier_keyword_fallback_enabled = _parse_bool(
        src.get("YOGA_CLASSIFIER_KEYWORD_FALLBACK_ENABLED")
        or src.get("yoga_classifier_keyword_fallback_enabled"),
        True,
    )
    yoga_classifier_keyword_fallback_terms = (
        src.get("YOGA_CLASSIFIER_KEYWORD_FALLBACK_TERMS")
        or src.get("yoga_classifier_keyword_fallback_terms")
        or "yoga,ヨガ"
    )
    yoga_classifier_keyword_fallback_on_errors = (
        src.get("YOGA_CLASSIFIER_KEYWORD_FALLBACK_ON_ERRORS")
        or src.get("yoga_classifier_keyword_fallback_on_errors")
        or "insufficient_quota,rate_limit"
    )

    return Config(
        gmail_query=gmail_query,
        google_client_secret_path=google_client_secret_path,
        google_token_path=google_token_path,
        yogisync_calendar_id=yogisync_calendar_id,
        timezone=timezone,
        sqlite_path=sqlite_path,
        default_event_duration_minutes=default_event_duration_minutes,
        openai_api_key=openai_api_key,
        openai_model_yoga_classifier=openai_model_yoga_classifier,
        yoga_classifier_enabled=yoga_classifier_enabled,
        yoga_classifier_max_chars=yoga_classifier_max_chars,
        yoga_classifier_fail_open=yoga_classifier_fail_open,
        yoga_classifier_min_confidence=yoga_classifier_min_confidence,
        yoga_classifier_keyword_fallback_enabled=yoga_classifier_keyword_fallback_enabled,
        yoga_classifier_keyword_fallback_terms=yoga_classifier_keyword_fallback_terms,
        yoga_classifier_keyword_fallback_on_errors=yoga_classifier_keyword_fallback_on_errors,
    )
