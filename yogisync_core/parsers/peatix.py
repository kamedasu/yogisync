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


def _extract_line_after(text: str, marker: str, lookahead: int = 8) -> Optional[str]:
    """Find the first non-empty line after an exact marker line."""
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line == marker:
            for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
                if lines[j]:
                    return lines[j]
    return None


def _cleanup_peatix_title(s: Optional[str]) -> Optional[str]:
    """
    ユーザー希望:
      - 「ヨガと本格的南インドカレーと」 ～ Spine Twist Spicy Curry Yoga を残す
      - 末尾の「のチケットお申し込み詳細」を削る
      - 末尾の（会場）を削る
      - 先頭の【Peatix】や[Peatix]を削る
    """
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()

    # remove leading peatix markers
    s = re.sub(r"^\s*【\s*Peatix\s*】\s*", "", s).strip()
    s = re.sub(r"^\s*\[\s*Peatix\s*\]\s*", "", s).strip()

    # remove trailing "...のチケットお申し込み詳細"
    s = re.sub(r"\s*のチケット(お申し込み)?詳細\s*$", "", s).strip()

    # remove trailing venue in parentheses
    s = re.sub(r"\s*[（(].+?[)）]\s*$", "", s).strip()

    return s or None


def _extract_title_from_body(text: str) -> Optional[str]:
    """
    Gmailカード表示（スクショ）に合わせて最優先:
      1) 「受信トレイ」の次行（イベント名 + (会場) が載りがち）→ cleanupで(会場)落とす
      2) 「予定のタイトル」の次行
      3) extract_label_value fallback
    """
    raw = first_non_empty(
        _extract_line_after(text, "受信トレイ"),
        _extract_line_after(text, "予定のタイトル"),
        extract_label_value(text, "予定のタイトル"),
    )
    return _cleanup_peatix_title(raw)


def _extract_reservation_id(text: str) -> Optional[str]:
    """
    予約/確認番号は揺れがあるので regex 直抜きを最優先にする。
    例:
      確認番号 34041688
      確認番号:34041688
      確認番号：34041688
    """
    m = re.search(r"(確認番号|予約番号)\s*[:：]?\s*([0-9]{5,})", text)
    if m:
        return m.group(2)

    rid = first_non_empty(
        _extract_line_after(text, "確認番号"),
        _extract_line_after(text, "予約番号"),
        extract_label_value(text, "確認番号"),
        extract_label_value(text, "予約番号"),
    )
    if not rid:
        return None

    digits = re.sub(r"\D+", "", rid.strip())
    return digits or rid.strip()


def _extract_address(text: str) -> Optional[str]:
    """
    住所は extract_label_value を優先、ダメなら regex で補強
    """
    addr = first_non_empty(
        extract_label_value(text, "住所"),
        extract_label_value(text, "所在地"),
    )
    if addr:
        return re.sub(r"\s+", " ", addr).strip()

    # fallback: "住所 ..." が同一行になってるケース
    m = re.search(r"住所\s*[:：]?\s*(.+)", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    return None


def parse_peatix(msg: GmailMessage) -> Optional[Event]:
    html = msg.text_html or ""
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    # title
    title = _extract_title_from_body(text)
    if not title:
        title = _cleanup_peatix_title(msg.subject)

    # datetime
    date = parse_first_datetime(text)
    if not date:
        return None

    # venue
    venue = first_non_empty(
        extract_label_value(text, "会場"),
        extract_label_value(text, "場所"),
    )

    # address
    address = _extract_address(text)

    # reservation id (confirmation number)
    reservation_id = _extract_reservation_id(text)

    source_url = _extract_peatix_url(soup)

    return Event(
        provider="peatix",
        title=title or "Peatix Event",
        date=date,
        location_name=venue,
        address=address,  # ← 住所をEvent(JSON/DB)に登録
        instructor=None,
        reservation_id=reservation_id,  # ← 確認番号を reservation_id に登録
        source_url=source_url,
        confidence=1.0 if title and reservation_id else 0.9,
    )
