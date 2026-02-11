from __future__ import annotations

from datetime import timedelta
from typing import Optional, List, Dict, Any, Tuple

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config
from .models import Event

# NOTE:
# token.json を 1つで運用しているなら、モジュールごとに scope がズレると 403 になりがちなので
# Gmail/Calendar 両方を入れておくのが安全（あなたの運用に合わせている）
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


def _build_gcal_time_range(config: Config, event: Event) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Google Calendar API の start/end フォーマットを組み立てる
    """
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
    """
    既存の eventId が分かっている場合：update
    無い場合：insert
    """
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
    """
    description に入っている event_uid をキーに、該当イベントを検索して返す。

    - q=event_uid で全文検索
    - timeMin/timeMax で日付近辺に絞る（検索精度＆速度UP）
    """
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)
    event_uid = event.ensure_event_uid()

    # 日付周辺だけを見る（大きいカレンダーだと重要）
    if event.time_unknown:
        # all-day は date ベースなので、前後数日で十分
        center = event.date.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        center = event.date

    time_min = (center - timedelta(days=7)).isoformat()
    time_max = (center + timedelta(days=7)).isoformat()

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

    # 念のため「本当に event_uid を含む」ものに絞る（q検索は緩いことがある）
    filtered: List[Dict[str, Any]] = []
    for it in items:
        desc = it.get("description") or ""
        if event_uid in desc:
            filtered.append(it)

    return filtered


def _choose_keep_event_id(events: List[Dict[str, Any]]) -> str:
    """
    重複がある場合に「残す1件」を決める。
    ざっくり安定するように：
    - updated が新しいものを優先
    - updated が無ければ id の辞書順
    """
    def key(it: Dict[str, Any]) -> Tuple[str, str]:
        return (it.get("updated") or "", it.get("id") or "")

    events_sorted = sorted(events, key=key, reverse=True)
    keep_id = events_sorted[0].get("id")
    if not keep_id:
        # 理論上ほぼ無いが保険
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
    """
    “重複しない” を強制するための統合関数。

    1) Calendar 側に event_uid が存在するか検索
    2) 1件なら update（または stored id があればそれ優先で update）
    3) 複数件なら、残す1件を決めて他は delete（cleanup_duplicates=True の場合）
    4) 0件なら insert（allow_create=True の場合）

    戻り値:
      - 最終的に採用した gcal_event_id（作成/更新/保持）
      - allow_create=False で 0件なら None
    """
    if not config.yogisync_calendar_id:
        raise ValueError("YOGISYNC_CALENDAR_ID is not set")

    service = get_calendar_service(config)
    body = _build_event_body(config, event)

    # まず event_uid で検索（既存を拾う）
    found = _find_events_by_event_uid(config, event)

    # 既にDBにgcal_event_idがあるなら、それが found の中にあるかも見る
    stored_in_found = False
    if stored_gcal_event_id:
        for it in found:
            if it.get("id") == stored_gcal_event_id:
                stored_in_found = True
                break

    # 0件：新規作成（許可されていれば）
    if not found:
        if not allow_create:
            return None
        created = (
            service.events()
            .insert(calendarId=config.yogisync_calendar_id, body=body)
            .execute()
        )
        return created.get("id")

    # 1件：それを update（ただし stored id があるなら stored を優先）
    if len(found) == 1:
        existing_id = found[0].get("id")
        target_id = stored_gcal_event_id or existing_id
        if not target_id:
            # 保険
            target_id = existing_id

        updated = (
            service.events()
            .update(calendarId=config.yogisync_calendar_id, eventId=target_id, body=body)
            .execute()
        )
        return updated.get("id")

    # 複数件：重複掃除
    keep_id: str
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

    # 残す1件を最新情報で update
    updated = (
        service.events()
        .update(calendarId=config.yogisync_calendar_id, eventId=keep_id, body=body)
        .execute()
    )
    return updated.get("id")
