from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from ..models import Event, GmailMessage
from . import extract_label_value, extract_url, parse_first_datetime, first_non_empty


# BONNE (hacomono) の「日時：2025年06月20日(金) 19:30~20:30」等を拾う
_BONNE_DATETIME_RE = re.compile(
    r"日時\s*[:：]\s*"
    r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"
    r"(?:\s*\([^)]*\))?\s*"
    r"(?P<h>\d{1,2})\s*:\s*(?P<min>\d{2})"
)


# 「スタッフ： Maya」などを拾う（全角/半角コロン、空白ゆれ対応）
_BONNE_STAFF_RE = re.compile(r"スタッフ\s*[:：]?\s*(?P<name>.+)")


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


def _extract_staff_name(text: str) -> Optional[str]:
    """
    BONNEは「インストラクター」ではなく「スタッフ」で来ることが多い。
    ラベル抽出で取れない場合もあるので、regexで補強する。
    """
    staff = first_non_empty(
        extract_label_value(text, "スタッフ"),
        extract_label_value(text, "インストラクター"),
        extract_label_value(text, "講師"),
    )
    if staff:
        return staff.strip() or None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _BONNE_STAFF_RE.match(line)
        if m:
            name = m.group("name").strip()
            # 変な追記が混ざる場合は軽く整形
            name = re.sub(r"\s{2,}", " ", name).strip()
            return name or None
    return None


def parse_bonne(msg: GmailMessage) -> Optional[Event]:
    text = msg.text_plain or msg.text_html or ""
    if not text:
        return None

    # 1) 汎用パーサ
    date = parse_first_datetime(text)
    # 2) BONNE専用の「日時：」行
    if not date:
        date = _parse_bonne_datetime(text)
    if not date:
        return None

    studio_name = first_non_empty(
        extract_label_value(text, "店舗"),
        "スタジオBONNE",
        "YOGA STUDIO BONNE",
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

    # ★講師（スタッフ）を Event.instructor に入れる
    instructor = _extract_staff_name(text)

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
        instructor=instructor,  # ← Maya が入る
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0,
    )
