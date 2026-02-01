from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

from dateutil import parser, tz

JST = tz.gettz("Asia/Tokyo")


def normalize_jp_datetime(text: str) -> str:
    return (
        text.replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
        .replace("時", ":")
        .replace("分", "")
    )


def parse_first_datetime(text: str) -> Optional[datetime]:
    if not text:
        return None
    candidate = normalize_jp_datetime(text)
    patterns = [
        r"\d{4}/\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}",
        r"\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}",
        r"\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2}",
    ]
    for pat in patterns:
        match = re.search(pat, candidate)
        if match:
            try:
                dt = parser.parse(match.group(0), dayfirst=False, yearfirst=True)
                return dt.replace(tzinfo=JST)
            except Exception:
                continue
    return None


def parse_first_date_only(text: str) -> Optional[datetime]:
    if not text:
        return None
    candidate = normalize_jp_datetime(text)
    patterns = [
        r"\d{4}/\d{1,2}/\d{1,2}",
        r"\d{4}-\d{1,2}-\d{1,2}",
        r"\d{1,2}/\d{1,2}",
    ]
    for pat in patterns:
        match = re.search(pat, candidate)
        if match:
            try:
                dt = parser.parse(match.group(0), dayfirst=False, yearfirst=True)
                return dt.replace(tzinfo=JST)
            except Exception:
                continue
    return None


def extract_label_value(text: str, label: str) -> Optional[str]:
    pattern = rf"{re.escape(label)}\s*[:：]\s*(.+)"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()
    return None


def extract_url(text: str) -> Optional[str]:
    match = re.search(r"https?://[^\s>]+", text)
    if match:
        return match.group(0)
    return None


def first_non_empty(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v:
            return v
    return None
