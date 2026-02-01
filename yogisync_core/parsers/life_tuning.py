from __future__ import annotations

from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_date_only, parse_first_datetime, first_non_empty


def parse_life_tuning(msg: GmailMessage) -> Optional[Event]:
    raw = msg.text_plain or msg.text_html or ""
    if not raw:
        return None

    if msg.text_html and not msg.text_plain:
        soup = BeautifulSoup(raw, "lxml")
        text = soup.get_text("\n")
    else:
        text = raw

    date = parse_first_datetime(text)
    time_unknown = False
    confidence = 1.0

    if not date:
        date_only = parse_first_date_only(text)
        if not date_only:
            return None
        date = date_only.replace(hour=12, minute=0)
        time_unknown = True
        confidence = 0.5

    title = first_non_empty(
        extract_label_value(text, "商品名"),
        extract_label_value(text, "イベント"),
        msg.subject,
        "LIFE TUNING DAYS",
    )

    source_url = extract_url(text)

    return Event(
        provider="life_tuning",
        title=title or "LIFE TUNING DAYS",
        date=date,
        location_name=extract_label_value(text, "会場"),
        address=extract_label_value(text, "住所"),
        instructor=None,
        reservation_id=extract_label_value(text, "注文番号"),
        source_url=source_url,
        confidence=confidence,
        time_unknown=time_unknown,
    )
