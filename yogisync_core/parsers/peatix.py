from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Event, GmailMessage
from . import extract_label_value, parse_first_datetime, first_non_empty


def _extract_peatix_url(soup: BeautifulSoup) -> Optional[str]:
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "peatix.com/event" in href:
            return href
    return None


def parse_peatix(msg: GmailMessage) -> Optional[Event]:
    html = msg.text_html or ""
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        title = soup.title.get_text(strip=True) if soup.title else None

    date = parse_first_datetime(text)
    if not date:
        return None

    venue = first_non_empty(
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )

    address = first_non_empty(
        extract_label_value(text, "住所"),
        extract_label_value(text, "所在地"),
    )

    source_url = _extract_peatix_url(soup)

    return Event(
        provider="peatix",
        title=title or msg.subject or "Peatix Event",
        date=date,
        location_name=venue,
        address=address,
        instructor=None,
        reservation_id=None,
        source_url=source_url,
        confidence=1.0 if title and venue else 0.8,
    )
