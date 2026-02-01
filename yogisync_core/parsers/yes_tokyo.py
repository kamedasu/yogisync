from __future__ import annotations

from typing import Optional

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


def parse_yes_tokyo(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    date = parse_first_datetime(text)
    if not date:
        return None

    title = first_non_empty(
        extract_label_value(text, "クラス"),
        extract_label_value(text, "プログラム"),
        msg.subject,
        "YES TOKYO Reservation",
    )

    reservation_id = first_non_empty(
        extract_label_value(text, "予約番号"),
        extract_label_value(text, "予約ID"),
    )

    location_name = first_non_empty(
        extract_label_value(text, "店舗"),
        "YES TOKYO STUDIO",
    )

    source_url = extract_url(text)

    return Event(
        provider="yes_tokyo",
        title=title or "YES TOKYO Class",
        date=date,
        location_name=location_name,
        address=None,
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0,
    )
