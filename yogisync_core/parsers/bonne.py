from __future__ import annotations

import re
from typing import Optional

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


def parse_bonne(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    date = parse_first_datetime(text)
    if not date:
        return None

    title = first_non_empty(
        extract_label_value(text, "プログラム"),
        extract_label_value(text, "クラス"),
        msg.subject,
        "Studio BONNE Reservation",
    )

    instructor = first_non_empty(
        extract_label_value(text, "インストラクター"),
        extract_label_value(text, "講師"),
    )

    reservation_id = first_non_empty(
        extract_label_value(text, "予約番号"),
        extract_label_value(text, "予約ID"),
    )

    source_url = extract_url(text)

    return Event(
        provider="bonne",
        title=title or "BONNE Class",
        date=date,
        location_name="スタジオBONNE",
        address=None,
        instructor=instructor,
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0,
    )
