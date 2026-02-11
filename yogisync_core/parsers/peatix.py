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


def _extract_line_after(text: str, marker: str, lookahead: int = 6) -> Optional[str]:
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line == marker:
            for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
                if lines[j]:
                    return lines[j]
    return None


def _extract_reservation_id(text: str) -> Optional[str]:
    rid = first_non_empty(
        _extract_line_after(text, "確認番号"),
        _extract_line_after(text, "予約番号"),
        extract_label_value(text, "確認番号"),
        extract_label_value(text, "予約番号"),
    )
    if not rid:
        return None
    rid = rid.strip()
    digits = re.sub(r"\D+", "", rid)
    return digits or rid


def _cleanup_peatix_title(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()

    # 先頭の【Peatix】や[Peatix]っぽいのを除去
    s = re.sub(r"^\s*【\s*Peatix\s*】\s*", "", s).strip()
    s = re.sub(r"^\s*\[\s*Peatix\s*\]\s*", "", s).strip()

    # 末尾の「のチケットお申し込み詳細」系を除去
    s = re.sub(r"\s*のチケット(お申し込み)?詳細\s*$", "", s).strip()

    # 末尾の会場括弧（例： （Andhra Dining））を除去 → ユーザー希望
    s = re.sub(r"\s*[（(].+?[)）]\s*$", "", s).strip()

    return s or None


def _extract_title_from_body(text: str) -> Optional[str]:
    """
    スクショの Gmail カード構造に合わせる：
      1) 「受信トレイ」の次行 → イベント名＋（会場）
      2) 「予定のタイトル」の次行 → イベント名（改行で Yoga が落ちることもある）
    優先は 1)（スクショの “いけてるタイトル” に一番近い）
    """
    t = first_non_empty(
        _extract_line_after(text, "受信トレイ"),
        _extract_line_after(text, "予定のタイトル"),
        extract_label_value(text, "予定のタイトル"),
    )
    return _cleanup_peatix_title(t)


def parse_peatix(msg: GmailMessage) -> Optional[Event]:
    html = msg.text_html or ""
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    # まず本文（Gmailカード部分）からタイトルを取る
    title = _extract_title_from_body(text)

    # 取れなければ subject を使う（ただし末尾の詳細は落とす）
    if not title:
        title = _cleanup_peatix_title(msg.subject)

    # 日時
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

    reservation_id = _extract_reservation_id(text)
    source_url = _extract_peatix_url(soup)

    return Event(
        provider="peatix",
        title=title or "Peatix Event",
        date=date,
        location_name=venue,
        address=address,
        instructor=None,
        reservation_id=reservation_id,
        source_url=source_url,
        confidence=1.0 if title and reservation_id else 0.85,
    )
