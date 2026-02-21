from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


# BONNE (hacomono) の「日時：2025年06月20日(金) 19:30~20:30」等を拾う
# ・月日が1桁/2桁どちらも対応
# ・区切り "~" と "～" の両方対応
# ・曜日 "(金)" などは無視
_BONNE_DATETIME_RE = re.compile(
    r"日時\s*[:：]\s*"
    r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"
    r"(?:\s*\([^)]*\))?\s*"
    r"(?P<h>\d{1,2})\s*:\s*(?P<min>\d{2})"
)


def _parse_bonne_datetime(text: str) -> Optional[datetime]:
    m = _BONNE_DATETIME_RE.search(text)
    if not m:
        return None
    try:
        return datetime(
            int(m.group("y")),
            int(m.group("m")),
            int(m.group("d")),
            int(m.group("h")),
            int(m.group("min")),
            0,
        )
    except ValueError:
        return None


def parse_bonne(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    # 1) まず既存の汎用パーサを試す
    date = parse_first_datetime(text)

    # 2) ダメなら BONNE専用の「日時：」行を拾う
    if not date:
        date = _parse_bonne_datetime(text)

    if not date:
        return None

    studio_name = first_non_empty(
        extract_label_value(text, "店舗"),
        "スタジオBONNE",
    )

    room = first_non_empty(
        extract_label_value(text, "ルーム"),
        extract_label_value(text, "部屋"),
    )

    title = first_non_empty(
        extract_label_value(text, "プログラム"),
        extract_label_value(text, "クラス"),
        msg.subject,
        "Studio BONNE Reservation",
    )

    # BONNEは「スタッフ：」が多い
    instructor = first_non_empty(
        extract_label_value(text, "インストラクター"),
        extract_label_value(text, "講師"),
        extract_label_value(text, "スタッフ"),
    )

    reservation_id = first_non_empty(
        extract_label_value(text, "予約番号"),
        extract_label_value(text, "予約ID"),
    )

    source_url = extract_url(text)

    location_name = studio_name
    if room:
        location_name = f"{studio_name} / {room}"

    return Event(
        provider="bonne",
        title=title or "BONNE Class",
        date=date,
        location_name=location_name,
        address=None,
        instructor=instructor,
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0,
    )
