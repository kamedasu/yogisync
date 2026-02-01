from __future__ import annotations

from typing import Optional

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


def parse_mosh(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    date = parse_first_datetime(text)
    if not date:
        return None

    title = first_non_empty(
        extract_label_value(text, "サービス"),
        extract_label_value(text, "メニュー"),
        msg.subject,
        "MOSH Reservation",
    )

    source_url = extract_url(text)

    return Event(
        provider="mosh",
        title=title or "MOSH Reservation",
        date=date,
        location_name=None,
        address=None,
        instructor=None,
        reservation_id=None,
        source_url=source_url,
        confidence=0.8,
    )
