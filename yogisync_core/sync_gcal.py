from __future__ import annotations

from datetime import timedelta
from typing import Optional, List, Dict, Any, Tuple
from zoneinfo import ZoneInfo

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
        f"instructor: {event.instructor or ''}",
        f"source_url: {event.source_url or ''}",
        f"address: {event.address or ''}",
    ]

    if event.base_price:
        lines.append(f"base_price: {event.base_price}")

    if event.option_menus:
        lines.append("option_menus:")
        for opt in event.option_menus:
            lines.append(f"- {opt}")

    if event.ticket_types:
        lines.append("ticket_types:")
        for t in event.ticket_types:
            lines.append(f"- {t}")

    lines.append(f"confidence: {event.confidence}")

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


def _ensure_tz_aware(dt, tz_name: str):
    tz = ZoneInfo(tz_name)
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _to_rfc3339(dt, tz_name: str) -> str:
    return _ensure_tz_aware(dt, tz_name).isoformat()


def _build_gcal_time_range(config: Config, event: Event) -> Tuple[Dict[str, str], Dict[str, str]]:
    if event.time_unknown:
        start_date = event.date.date()
        end_date = start_date + timedelta(days=1)
        start = {"date": start_date.isoformat()}
        end = {"date": end_date.isoformat()}
        return start, end

    start_dt = event.date
    end_dt = start_dt + timedelta(minutes=config.default_event_duration_minutes)
    start = {"dateTime": start_dt.isoformat(), "timeZone": config.timezone}
    end = {"dateTime": end_dt.isoformat(), "timeZone": config.timezone}
    return start, end


def _build_event_body(config: Config, event: Event) -> Dict[str, Any]:
    start, end = _build_gcal_time_range(config, event)

    body: Dict[str, Any] = {
        "summary": build_summary(event),
        "description": build_description(event),
        "start": start,
        "end": end,
    }

    location = build_location(event)
    if location:
        body["location"] = location

    return body


def upsert_event(config: Config, event: Event, gcal_event_id: Optional[str]) -> str:
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)
    body = _build_event_body(config, event)

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


def _find_events_by_event_uid(config: Config, event: Event) -> List[Dict[str, Any]]:
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)
    event_uid = event.ensure_event_uid()

    if event.time_unknown:
        center = event.date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        center = event.date

    time_min = _to_rfc3339(center - timedelta(days=7), config.timezone)
    time_max = _to_rfc3339(center + timedelta(days=7), config.timezone)

    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        resp = (
            service.events()
            .list(
                calendarId=config.yogisync_calendar_id,
                q=event_uid,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    filtered: List[Dict[str, Any]] = []
    for it in items:
        desc = it.get("description") or ""
        if event_uid in desc:
            filtered.append(it)

    return filtered


def _choose_keep_event_id(events: List[Dict[str, Any]]) -> str:
    def key(it: Dict[str, Any]) -> Tuple[str, str]:
        return (it.get("updated") or "", it.get("id") or "")

    events_sorted = sorted(events, key=key, reverse=True)
    keep_id = events_sorted[0].get("id")
    if not keep_id:
        keep_id = events[0].get("id")
    if not keep_id:
        raise ValueError("Could not determine keep event id (missing id)")
    return keep_id


def reconcile_event(
    config: Config,
    event: Event,
    stored_gcal_event_id: Optional[str],
    *,
    allow_create: bool = True,
    cleanup_duplicates: bool = True,
) -> Optional[str]:
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)
    body = _build_event_body(config, event)

    found = _find_events_by_event_uid(config, event)

    stored_in_found = False
    if stored_gcal_event_id:
        for it in found:
            if it.get("id") == stored_gcal_event_id:
                stored_in_found = True
                break

    if not found:
        if not allow_create:
            return None
        created = (
            service.events()
            .insert(calendarId=config.yogisync_calendar_id, body=body)
            .execute()
        )
        return created.get("id")

    if len(found) == 1:
        existing_id = found[0].get("id")
        target_id = stored_gcal_event_id or existing_id
        if not target_id:
            target_id = existing_id

        updated = (
            service.events()
            .update(calendarId=config.yogisync_calendar_id, eventId=target_id, body=body)
            .execute()
        )
        return updated.get("id")

    if stored_gcal_event_id and stored_in_found:
        keep_id = stored_gcal_event_id
    else:
        keep_id = _choose_keep_event_id(found)

    if cleanup_duplicates:
        for it in found:
            eid = it.get("id")
            if not eid or eid == keep_id:
                continue
            service.events().delete(calendarId=config.yogisync_calendar_id, eventId=eid).execute()

    updated = (
        service.events()
        .update(calendarId=config.yogisync_calendar_id, eventId=keep_id, body=body)
        .execute()
    )
    return updated.get("id")
