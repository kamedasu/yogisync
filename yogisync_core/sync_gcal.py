from __future__ import annotations

from datetime import timedelta
from typing import Optional

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config
from .models import Event

SCOPES_CAL = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

def get_calendar_service(config: Config):
    creds = get_credentials(SCOPES_CAL, config.google_client_secret_path, config.google_token_path)
    return build("calendar", "v3", credentials=creds)


def build_description(event: Event) -> str:
    lines = [
        f"provider: {event.provider}",
        f"event_uid: {event.ensure_event_uid()}",
        f"reservation_id: {event.reservation_id or ''}",
        f"source_url: {event.source_url or ''}",
        f"confidence: {event.confidence}",
    ]
    if event.time_unknown:
        lines.append("time_unknown: true (needs confirmation)")
    return "\n".join(lines)


def build_summary(event: Event) -> str:
    provider = event.provider.upper()
    base = f"[{provider}] {event.title}"
    if event.instructor:
        return f"{base} - {event.instructor}"
    return base


def build_location(event: Event) -> Optional[str]:
    if event.location_name and event.address:
        return f"{event.location_name} / {event.address}"
    return event.location_name or event.address


def upsert_event(config: Config, event: Event, gcal_event_id: Optional[str]) -> str:
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)

    if event.time_unknown:
        start_date = event.date.date()
        end_date = start_date + timedelta(days=1)
        start = {"date": start_date.isoformat()}
        end = {"date": end_date.isoformat()}
    else:
        start_dt = event.date
        end_dt = start_dt + timedelta(minutes=config.default_event_duration_minutes)
        start = {"dateTime": start_dt.isoformat(), "timeZone": config.timezone}
        end = {"dateTime": end_dt.isoformat(), "timeZone": config.timezone}

    body = {
        "summary": build_summary(event),
        "description": build_description(event),
        "start": start,
        "end": end,
    }

    location = build_location(event)
    if location:
        body["location"] = location

    if gcal_event_id:
        updated = (
            service.events()
            .update(calendarId=config.yogisync_calendar_id, eventId=gcal_event_id, body=body)
            .execute()
        )
        return updated.get("id")

    created = (
        service.events()
        .insert(calendarId=config.yogisync_calendar_id, body=body)
        .execute()
    )
    return created.get("id")
