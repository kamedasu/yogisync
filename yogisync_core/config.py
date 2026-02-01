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

    return Config(
        gmail_query=gmail_query,
        google_client_secret_path=google_client_secret_path,
        google_token_path=google_token_path,
        yogisync_calendar_id=yogisync_calendar_id,
        timezone=timezone,
        sqlite_path=sqlite_path,
    )
